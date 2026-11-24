"""Choose local Go packages for the offline validator."""

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

GO_IMAGE = "golang@sha256:8bee1901f1e530bfb4a7850aa7a479d17ae3a18beb6e09064ed54cfd245b7191"
GO_TMPFS = "rw,exec,nosuid,nodev,size=384m"
GO_ENV = (
    "CGO_ENABLED=0",
    "GOCACHE=/tmp/go-cache",
    "GOPATH=/tmp/go",
    "GOENV=off",
    "GOWORK=off",
    "GOTOOLCHAIN=local",
    "GOPROXY=off",
    "GOSUMDB=off",
)


@dataclass(frozen=True)
class GoInvocation:
    directory: str
    arguments: list[str]
    standalone: bool


def go_source_path(path: str) -> bool:
    return path.endswith(".go") or PurePosixPath(path).name == "go.mod"


def go_invocation(
    root: Path, paths: list[str], deleted: frozenset[str] = frozenset()
) -> GoInvocation:
    """Build `go test` for the packages behind paths.

    A deleted file tests its remaining package. When no Go files remain in that package,
    the whole module is tested so code that imported it is still built.
    """
    packages = sorted({PurePosixPath(path).parent for path in paths})
    modules = set()
    for package in packages:
        for directory in (package, *package.parents):
            manifest = root / directory / "go.mod"
            if manifest.is_file() and manifest.resolve() == manifest:
                modules.add(directory)
                break
        else:
            modules.add(None)
    if len(modules) != 1:
        raise ValueError(
            "Go validation currently needs changes within one module or standalone tree."
        )
    module = modules.pop()
    standalone = module is None
    directory = module or PurePosixPath(".")
    arguments = ["test", "-count=1", "-timeout=45s"]
    if not standalone:
        arguments.append("-mod=readonly")
    go_files = [PurePosixPath(path) for path in paths if path.endswith(".go")]
    removed_package = any(
        path.as_posix() in deleted and not any(root.joinpath(*path.parent.parts).glob("*.go"))
        for path in go_files
    )
    if removed_package or all(PurePosixPath(path).name == "go.mod" for path in paths):
        arguments.append("./...")
    else:
        go_packages = sorted({path.parent for path in go_files})
        arguments.extend(
            "." if package == directory else f"./{package.relative_to(directory)}"
            for package in go_packages
        )
    return GoInvocation((PurePosixPath("/workspace") / directory).as_posix(), arguments, standalone)
