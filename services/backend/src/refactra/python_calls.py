"""Check statically resolvable calls to functions defined in the staged repository."""

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
            return tree
        except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
            return None

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
            if not names or not any(call.func.id in names for call in calls):
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
            source_bindings = _bindings(ast.walk(source))
            for definition in source.body:
                if (
                    isinstance(definition, _DEFINITIONS)
                    and not definition.decorator_list
                    and source_bindings[definition.name] == 1
                ):
                    for alias, name in names.items():
                        if name == definition.name:
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
