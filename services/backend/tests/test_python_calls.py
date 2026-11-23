from pathlib import PurePosixPath

import pytest

from refactra.python_calls import new_python_call_issue


@pytest.mark.parametrize(
    "source, old_import, expected",
    [
        ("def available(): pass\n", "available", "imports missing"),
        ("def available(): pass\n", "missing", None),
        ("def __getattr__(name): return name\n", "available", None),
        ("from external_plugin import *\n", "available", None),
        ("missing = 'exported'\n", "available", None),
    ],
)
def test_only_new_statically_missing_module_exports_are_reported(
    tmp_path, source, old_import, expected
):
    root = tmp_path.resolve()
    (root / "helpers.py").write_text(source, encoding="utf-8")
    (root / "caller.py").write_text(f"from helpers import {old_import}\n", encoding="utf-8")
    result = new_python_call_issue(
        root, {PurePosixPath("caller.py"): b"from helpers import missing\n# Updated file.\n"}
    )
    if expected is None:
        assert result is None
    else:
        assert expected in result
