"""Check statically resolvable imports and calls in the staged repository."""

import ast
import warnings
from collections import Counter
from functools import lru_cache
from inspect import Parameter, Signature
from pathlib import Path, PurePosixPath

from refactra.python_symbols import MAX_NODES_PER_FILE, python_import_module

MAX_SOURCE_BYTES = 64 * 1024
MAX_IMPORTED_MODULES = 64
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DYNAMIC_EXPORTS = {
    "__getattr__",
    "__dict__",
    "eval",
    "exec",
    "globals",
    "locals",
    "setattr",
    "vars",
}
_DYNAMIC_ATTRIBUTES = _DYNAMIC_EXPORTS | {"modules"}


def _signature(arguments: ast.arguments) -> Signature:
    params = []
    positional = [*arguments.posonlyargs, *arguments.args]
    required = len(positional) - len(arguments.defaults)
    for index, item in enumerate(positional):
        kind = (
            Parameter.POSITIONAL_ONLY
            if index < len(arguments.posonlyargs)
            else Parameter.POSITIONAL_OR_KEYWORD
        )
        params.append(
            Parameter(item.arg, kind, default=Parameter.empty if index < required else None)
        )
    if arguments.vararg:
        params.append(Parameter(arguments.vararg.arg, Parameter.VAR_POSITIONAL))
    for item, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
        params.append(
            Parameter(
                item.arg,
                Parameter.KEYWORD_ONLY,
                default=Parameter.empty if default is None else None,
            )
        )
    if arguments.kwarg:
        params.append(Parameter(arguments.kwarg.arg, Parameter.VAR_KEYWORD))
    return Signature(params)


def _bindings(nodes) -> Counter:
    names = Counter()
    for node in nodes:
        if isinstance(node, ast.alias):
            names[node.asname or node.name.split(".")[0]] += 1
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names[node.id] += 1
        elif isinstance(node, ast.arg):
            names[node.arg] += 1
        elif isinstance(
            node, (*_DEFINITIONS, ast.ClassDef, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)
        ):
            names[node.name] += 1
        elif isinstance(node, ast.MatchMapping):
            names[node.rest] += 1
    return names


def _import_mode_tree(tree: ast.Module, path: PurePosixPath) -> ast.Module:
    """Follow an explicit package guard in the file's ordinary import mode."""
    body = []
    for statement in tree.body:
        if (
            isinstance(statement, ast.If)
            and isinstance(statement.test, ast.Name)
            and statement.test.id == "__package__"
        ):
            body.extend(statement.body if path.parent.parts else statement.orelse)
        else:
            body.append(statement)
    return ast.Module(body=body, type_ignores=tree.type_ignores)


def _dynamic_exports(nodes: tuple[ast.AST, ...], bindings: Counter) -> bool:
    return "__getattr__" in bindings or any(
        (isinstance(node, ast.alias) and node.name == "*")
        or (isinstance(node, ast.Name) and node.id in _DYNAMIC_EXPORTS)
        or (isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_ATTRIBUTES)
        for node in nodes
    )


def _issues(
    root: Path, paths: tuple[PurePosixPath, ...], overlay: dict[PurePosixPath, bytes | None]
):
    imported = set()

    @lru_cache(maxsize=MAX_IMPORTED_MODULES + 10)
    def read(path: PurePosixPath) -> ast.Module | None:
        target = root / path
        try:
            if path in overlay:
                data = overlay[path]
            elif target.is_file() and target.resolve() == target:
                with target.open("rb") as source:
                    data = source.read(MAX_SOURCE_BYTES + 1)
            else:
                return None
            if data is None or len(data) > MAX_SOURCE_BYTES:
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(data, filename=str(path))
            for index, _ in enumerate(ast.walk(tree), start=1):
                if index > MAX_NODES_PER_FILE:
                    return None
            return _import_mode_tree(tree, path)
        except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
            return None

    @lru_cache(maxsize=MAX_IMPORTED_MODULES)
    def exports(source: ast.Module):
        nodes = tuple(ast.walk(source))
        names = _bindings(nodes)
        definitions = {
            item.name: item
            for item in source.body
            if isinstance(item, _DEFINITIONS) and not item.decorator_list and names[item.name] == 1
        }
        return names, definitions, _dynamic_exports(nodes, names)

    for path in paths:
        tree = read(path)
        if tree is None:
            continue
        bindings = _bindings(ast.walk(tree))
        calls = [
            call
            for call in ast.walk(tree)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and not any(isinstance(arg, ast.Starred) for arg in call.args)
            and all(keyword.arg is not None for keyword in call.keywords)
        ]
        targets = {}
        for statement in tree.body:
            if not isinstance(statement, ast.ImportFrom):
                continue
            module = python_import_module(path, statement)
            if module is None or not module.parts:
                continue
            names = {
                item.asname or item.name: item.name
                for item in statement.names
                if bindings[item.asname or item.name] == 1
            }
            if not names:
                continue
            if module not in imported and len(imported) >= MAX_IMPORTED_MODULES:
                continue
            imported.add(module)
            source_path = module / "__init__.py"
            source = read(source_path)
            if source is None:
                source_path = module.with_suffix(".py")
                source = read(source_path)
            if source is None:
                continue
            source_bindings, definitions, dynamic = exports(source)
            # Packages may supply child modules and ordinary modules may populate names dynamically.
            exports_known = source_path.name != "__init__.py" and not dynamic
            for alias, name in names.items():
                if exports_known and name not in source_bindings and not name.startswith("__"):
                    yield (
                        (path, source_path, name, "missing_import"),
                        f"{path}:{statement.lineno} imports {name}, which is not defined "
                        f"in {source_path}.",
                    )
                if definition := definitions.get(name):
                    try:
                        targets[alias] = (source_path, name, _signature(definition.args))
                    except ValueError:
                        continue

        for call in calls:
            if call.func.id not in targets:
                continue
            source_path, name, signature = targets[call.func.id]
            try:
                signature.bind(
                    *([None] * len(call.args)), **{keyword.arg: None for keyword in call.keywords}
                )
            except TypeError as error:
                yield (
                    (path, source_path, name, str(error)),
                    f"{path}:{call.lineno} has an invalid call to "
                    f"{name}{signature} in {source_path}: {error}.",
                )


def new_python_call_issue(root: Path, overlay: dict[PurePosixPath, bytes | None]) -> str | None:
    """Return the first newly introduced error, without importing the repository."""
    paths = tuple(path for path, data in overlay.items() if data is not None)
    before = Counter(key for key, _ in _issues(root, paths, {}))
    for key, message in _issues(root, paths, overlay):
        if before[key] == 0:
            return message
        before[key] -= 1
    return None
