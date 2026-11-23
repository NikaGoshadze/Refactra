"""Parse retrieved Python into structural observations without executing repository code."""

import ast
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath

from refactra.repository_content import RetrievedContent, RetrievedFile, SkippedFile

MAX_NODES_PER_FILE = 10_000
MAX_RECORDS_PER_FILE = 200


@dataclass(frozen=True)
class PythonSymbol:
    name: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class PythonImport:
    module: str
    imported_name: str | None
    alias: str | None
    scope: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class PythonFileIndex:
    path: str
    role: str
    symbols: tuple[PythonSymbol, ...]
    imports: tuple[PythonImport, ...]
    issue: str | None = None
    issue_line: int | None = None


@dataclass(frozen=True)
class PythonInspection:
    repository_id: int
    commit_sha: str
    files: tuple[PythonFileIndex, ...]
    skipped: tuple[SkippedFile, ...]


class _InspectionLimit(Exception):
    pass


def python_import_module(path: PurePosixPath, statement: ast.ImportFrom) -> PurePosixPath | None:
    module = statement.module.split(".") if statement.module else []
    if not statement.level:
        return PurePosixPath(*module)
    parents = path.parent.parts
    if statement.level > len(parents):
        return None
    return PurePosixPath(*parents[: len(parents) - statement.level + 1], *module)


def _index_file(source: RetrievedFile) -> PythonFileIndex:
    try:
        with warnings.catch_warnings():
            # Parser warnings can echo source lines; diagnostics expose codes/locations only.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source.text, filename=source.path, mode="exec")
        stack, symbols, imports, visited = [(tree, ())], [], [], 0
        offset = source.start_line - 1
        while stack:
            node, scope = stack.pop()
            visited += 1
            if visited > MAX_NODES_PER_FILE:
                raise _InspectionLimit
            child_scope = scope
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = (
                    "class"
                    if isinstance(node, ast.ClassDef)
                    else "async_function"
                    if isinstance(node, ast.AsyncFunctionDef)
                    else "function"
                )
                start = min([node.lineno] + [item.lineno for item in node.decorator_list])
                child_scope = (*scope, node.name)
                symbols.append(
                    PythonSymbol(
                        node.name,
                        ".".join(child_scope),
                        kind,
                        start + offset,
                        (node.end_lineno or node.lineno) + offset,
                    )
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    module = (
                        alias.name
                        if isinstance(node, ast.Import)
                        else "." * node.level + (node.module or "")
                    )
                    name = None if isinstance(node, ast.Import) else alias.name
                    imports.append(
                        PythonImport(
                            module,
                            name,
                            alias.asname,
                            ".".join(scope),
                            node.lineno + offset,
                            (node.end_lineno or node.lineno) + offset,
                        )
                    )
            if len(symbols) + len(imports) > MAX_RECORDS_PER_FILE:
                raise _InspectionLimit
            stack.extend(
                (child, child_scope) for child in reversed(list(ast.iter_child_nodes(node)))
            )
        symbols.sort(key=lambda item: (item.start_line, item.qualified_name))
        imports.sort(
            key=lambda item: (
                item.start_line,
                item.module,
                item.imported_name or "",
                item.alias or "",
            )
        )
        return PythonFileIndex(source.path, source.role, tuple(symbols), tuple(imports))
    except SyntaxError as error:
        line = error.lineno + source.start_line - 1 if error.lineno is not None else None
        return PythonFileIndex(source.path, source.role, (), (), "syntax_error", line)
    except (RecursionError, _InspectionLimit):
        return PythonFileIndex(source.path, source.role, (), (), "inspection_limit")


def inspect_python_symbols(content: RetrievedContent) -> PythonInspection:
    """Consume policy-checked bounded retrieval, preserving its revision and omissions.

    No imports are resolved, decorators evaluated or source literals/docstrings returned.
    Parsed declarations are not a call graph, compiler validation or passing tests.
    """
    files, skipped = [], list(content.skipped)
    for source in sorted(content.files, key=lambda item: item.path):
        if PurePosixPath(source.path).suffix not in {".py", ".pyi"}:
            skipped.append(SkippedFile(source.path, "unsupported_language"))
            continue
        files.append(_index_file(source))
    return PythonInspection(content.repository_id, content.commit_sha, tuple(files), tuple(skipped))
