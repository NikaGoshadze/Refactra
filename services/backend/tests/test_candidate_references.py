from refactra.candidate_references import collect_candidate_references, valid_candidate_references


def test_references_follow_local_dependencies_without_including_unrelated_files(tmp_path):
    package = tmp_path / "library"
    package.mkdir()
    source = {"library/ui.py": "from library.service import search\n"}
    references = {
        "library/service.py": "from .formatting import format_results\n",
        "library/formatting.py": "from . import service\ndef format_results(): return []\n",
    }
    unrelated = {"library/unrelated.py": "from . import formatting\n"}
    for path, content in (references | unrelated).items():
        (tmp_path / path).write_bytes(content.encode())

    editable = set(source)
    assert collect_candidate_references(tmp_path, source, editable) == references
    assert valid_candidate_references(dict(reversed(references.items())), source, editable)
    assert not valid_candidate_references(references | unrelated, source, editable)
    assert not valid_candidate_references(
        {"library/formatting.py": references["library/formatting.py"]}, source, editable
    )
