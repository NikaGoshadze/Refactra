from dataclasses import FrozenInstanceError, replace

import pytest

from refactra import repository_content as content
from refactra.repository_inventory import inventory_snapshot
from refactra.repository_selection import FileSelection, SelectedFile, select_task_files
from refactra.repository_snapshot import SourceFile, SourceSnapshot


def fixture(files):
    snapshot = SourceSnapshot(42, "a" * 40, tuple(SourceFile(path, data) for path, data in files))
    references = tuple(SelectedFile(path, "source", (), len(data)) for path, data in files)
    return snapshot, FileSelection(42, "a" * 40, references, 0, 0, False)


def test_selected_source_tests_and_metadata_are_returned_with_provenance_and_no_execution():
    snapshot, _ = fixture(
        [
            ("src/cart.py", b"raise RuntimeError('never execute')\n"),
            ("tests/test_cart.py", b"assert True"),
            ("requirements.txt", b"pytest==8.3.3\n"),
        ]
    )
    selection = select_task_files(snapshot, inventory_snapshot(snapshot), "Refactor cart")
    result = content.retrieve_selected_content(snapshot, selection)
    assert (result.repository_id, result.commit_sha) == (42, "a" * 40)
    assert [(file.path, file.role) for file in result.files] == [
        ("src/cart.py", "source"),
        ("tests/test_cart.py", "test"),
        ("requirements.txt", "metadata"),
    ]
    assert [file.text for file in result.files] == [
        source.content.decode() for source in snapshot.files
    ]
    assert all(file.start_line == file.end_line == 1 for file in result.files)
    assert result.total_bytes == sum(len(source.content) for source in snapshot.files)
    assert not result.skipped and "never execute" not in repr(result)
    with pytest.raises(FrozenInstanceError):
        result.files[0].text = "changed"


@pytest.mark.parametrize(
    "text,lines",
    [
        ("first\nsecond\n", 2),
        ("first\r\nsecond", 2),
        ("first\rsecond\r", 2),
        ("\n", 1),
        ("value = '\u2028'\n", 1),
        ("\ufefffirst\n", 1),
    ],
)
def test_line_ranges_handle_physical_newlines_and_utf8_bom(text, lines):
    snapshot, selection = fixture([("src/cart.py", text.encode())])
    result = content.retrieve_selected_content(snapshot, selection)
    assert result.files[0].start_line == 1 and result.files[0].end_line == lines
    assert result.files[0].text == text.removeprefix("\ufeff")


@pytest.mark.parametrize(
    "path,data,reason",
    [
        ("src/secrets.py", b"hidden", "ineligible_path"),
        (".github/task.py", b"pass", "ineligible_path"),
        ("data.json", b"{}", "ineligible_path"),
        ("src/cart.py", b"\xff", "unsupported_text"),
        ("src/cart.py", b"text\x00", "unsupported_text"),
        ("src/cart.py", b"", "empty_file"),
        ("src/cart.py", b'API_KEY = "hidden-value"', "possible_secret"),
        ("src/cart.py", b'GITHUB_TOKEN: str = "hidden-value"', "possible_secret"),
        ("src/cart.py", b'config = {"password": "hidden-value"}', "possible_secret"),
        ("src/cart.py", b'KEY = "-----BEGIN RSA PRIVATE KEY-----"', "possible_secret"),
        ("src/cart.py", b'URL = "https://name:hidden-value@example.com"', "possible_secret"),
    ],
)
def test_rechecks_bytes_and_withholds_entire_files_with_explicit_reasons(path, data, reason):
    snapshot, selection = fixture([(path, data)])
    result = content.retrieve_selected_content(snapshot, selection)
    assert not result.files and result.total_bytes == 0
    assert [(file.path, file.reason) for file in result.skipped] == [(path, reason)]
    assert "hidden-value" not in repr(result)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"path": "missing.py"}, "missing_file"),
        ({"size_bytes": 0}, "size_mismatch"),
        ({"role": "metadata"}, "role_mismatch"),
    ],
)
def test_reference_fields_are_not_trusted(changes, reason):
    snapshot, selection = fixture([("src/cart.py", b"pass")])
    selection = replace(selection, files=(replace(selection.files[0], **changes),))
    assert content.retrieve_selected_content(snapshot, selection).skipped[0].reason == reason


@pytest.mark.parametrize("changes", [{"repository_id": 99}, {"commit_sha": "b" * 40}])
def test_wrong_revision_rejects_the_whole_request(changes):
    snapshot, selection = fixture([])
    with pytest.raises(ValueError, match="same revision"):
        content.retrieve_selected_content(snapshot, replace(selection, **changes))


@pytest.mark.parametrize("count", [2, 9])
def test_duplicate_and_excessive_references_are_rejected(count):
    snapshot, selection = fixture([("src/cart.py", b"pass")])
    references = (
        selection.files * 2
        if count == 2
        else tuple(
            replace(selection.files[0], path=f"src/file_{index}.py") for index in range(count)
        )
    )
    with pytest.raises(ValueError, match="unique"):
        content.retrieve_selected_content(snapshot, replace(selection, files=references))


def test_retrieval_enforces_its_own_actual_byte_budgets(monkeypatch):
    snapshot, selection = fixture(
        [
            ("src/a.py", b"1234"),
            ("src/b.py", b"1234"),
            ("src/c.py", b"12345"),
        ]
    )
    monkeypatch.setattr(content, "MAX_FILE_BYTES", 4)
    monkeypatch.setattr(content, "MAX_TOTAL_BYTES", 4)
    result = content.retrieve_selected_content(snapshot, selection)
    assert result.total_bytes == 4 and [file.path for file in result.files] == ["src/a.py"]
    assert [file.reason for file in result.skipped] == ["total_byte_limit", "file_byte_limit"]
