import warnings
from dataclasses import FrozenInstanceError, replace

import pytest

from refactra import python_symbols as symbols
from refactra.repository_content import RetrievedFile, SkippedFile, retrieve_selected_content
from refactra.repository_inventory import inventory_snapshot
from refactra.repository_selection import select_task_files
from refactra.repository_snapshot import SourceFile, SourceSnapshot


def retrieved(text, path="src/demo.py"):
    snapshot = SourceSnapshot(42, "a" * 40, (SourceFile(path, text.encode()),))
    selection = select_task_files(snapshot, inventory_snapshot(snapshot), "Inspect demo")
    return retrieve_selected_content(snapshot, selection)


def test_nested_decorated_definitions_and_imports_are_indexed_without_execution():
    text = """import os.path as operating, json
from ..helpers import (
    calculate as calc,
)
@explode("decorator-marker")
class Cart:
    @staticmethod
    async def total(self, value="literal-marker"):
        import missing_package as pkg
        def inner():
            return calc(value)
        return inner()
def factory():
    class Worker:
        pass
    return Worker
raise RuntimeError("never execute")
"""
    result = symbols.inspect_python_symbols(retrieved(text))
    assert (result.repository_id, result.commit_sha) == (42, "a" * 40)
    file = result.files[0]
    assert (file.path, file.role, file.issue) == ("src/demo.py", "source", None)
    assert [
        (item.qualified_name, item.kind, item.start_line, item.end_line) for item in file.symbols
    ] == [
        ("Cart", "class", 5, 12),
        ("Cart.total", "async_function", 7, 12),
        ("Cart.total.inner", "function", 10, 11),
        ("factory", "function", 13, 16),
        ("factory.Worker", "class", 14, 15),
    ]
    assert [(item.module, item.imported_name, item.alias, item.scope) for item in file.imports] == [
        ("json", None, None, ""),
        ("os.path", None, "operating", ""),
        ("..helpers", "calculate", "calc", ""),
        ("missing_package", None, "pkg", "Cart.total"),
    ]
    assert (file.imports[2].start_line, file.imports[2].end_line) == (2, 4)
    for literal in ("literal-marker", "decorator-marker", "never execute"):
        assert literal not in repr(result)
    with pytest.raises(FrozenInstanceError):
        file.symbols[0].name = "changed"


def test_relative_star_imports_and_stub_files_keep_declarations_without_resolution():
    result = symbols.inspect_python_symbols(
        retrieved(
            "from . import shared as s\nfrom package import *\ndef run() -> None: ...\n",
            "src/demo.pyi",
        )
    )
    file = result.files[0]
    assert not file.issue and file.symbols[0].name == "run"
    assert [(item.module, item.imported_name, item.alias) for item in file.imports] == [
        (".", "shared", "s"),
        ("package", "*", None),
    ]


def test_bad_syntax_is_sanitized_and_does_not_discard_other_files():
    bad = retrieved("def broken(sensitive-marker\n")
    good = retrieved("def valid():\n    return 1\n", "src/good.py")
    result = symbols.inspect_python_symbols(replace(bad, files=bad.files + good.files))
    bad_file, good_file = result.files
    assert (bad_file.issue, bad_file.issue_line) == ("syntax_error", 1)
    assert bad_file.symbols == bad_file.imports == ()
    assert good_file.symbols[0].name == "valid" and not good_file.issue
    assert "sensitive-marker" not in repr(result)


def test_parser_warnings_do_not_echo_source_diagnostics():
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter("always", SyntaxWarning)
        result = symbols.inspect_python_symbols(retrieved('value = "\\q"\n'))
    assert not result.files[0].issue and not emitted


@pytest.mark.parametrize("newlines", ["\n", "\r\n", "\r"])
def test_locations_respect_physical_newlines_and_the_retrieved_start_line(newlines):
    content = retrieved(newlines.join(["import os", "def run():", "    pass", ""]))
    content = replace(content, files=(replace(content.files[0], start_line=20, end_line=22),))
    file = symbols.inspect_python_symbols(content).files[0]
    assert (file.symbols[0].start_line, file.symbols[0].end_line) == (21, 22)
    assert file.imports[0].start_line == 20


@pytest.mark.parametrize("limit,value", [("MAX_NODES_PER_FILE", 1), ("MAX_RECORDS_PER_FILE", 1)])
def test_inspection_limits_discard_partial_file_indexes(monkeypatch, limit, value):
    monkeypatch.setattr(symbols, limit, value)
    content = retrieved("def first(): pass\ndef second(): pass\n")
    file = symbols.inspect_python_symbols(content).files[0]
    assert file.issue == "inspection_limit" and file.symbols == file.imports == ()


def test_an_index_at_the_record_limit_is_complete(monkeypatch):
    monkeypatch.setattr(symbols, "MAX_RECORDS_PER_FILE", 1)
    file = symbols.inspect_python_symbols(retrieved("def run(): pass\n")).files[0]
    assert not file.issue and len(file.symbols) == 1


def test_non_python_content_and_upstream_omissions_are_recorded():
    content = retrieved("pass\n")
    other = RetrievedFile("src/main.ts", "source", 1, 1, 10, "export {};")
    content = replace(
        content,
        files=content.files + (other,),
        skipped=(SkippedFile("src/secrets.py", "ineligible_path"),),
    )
    result = symbols.inspect_python_symbols(content)
    assert len(result.files) == 1 and result.files[0].symbols == ()
    assert [(item.path, item.reason) for item in result.skipped] == [
        ("src/secrets.py", "ineligible_path"),
        ("src/main.ts", "unsupported_language"),
    ]
    assert symbols.inspect_python_symbols(replace(content, files=())).files == ()
