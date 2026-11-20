import pytest

from refactra import repository_validation as validation
from refactra.refactoring_plan import ValidationCheck
from refactra.repository_workspace import ApprovedWorkspace


def test_validation_preflights_explicit_paths_before_docker(tmp_path, monkeypatch):
    check = ValidationCheck(
        check_id="python.pytest", paths=["--collect-only.py"], required=True, criteria_ids=["AC1"]
    )
    workspace = ApprovedWorkspace(tmp_path, "a" * 40, 1, "b" * 64, 0, (), (check,))
    monkeypatch.setattr(validation, "_docker", lambda *args: pytest.fail("Docker was called"))
    with pytest.raises(validation.RepositoryValidationError, match="staged Python files"):
        validation.validate_candidate(workspace)
    (tmp_path / "--collect-only.py").touch()
    assert validation._targets(workspace, check) == ["./--collect-only.py"]


def test_validation_refuses_to_start_with_network_access():
    with pytest.raises(validation.RepositoryValidationError, match="required validation isolation"):
        validation._verify_isolation({"HostConfig": {"NetworkMode": "bridge"}, "Mounts": []})
