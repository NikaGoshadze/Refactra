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
    unavailable, baseline = validation.validate_candidate(workspace)
    assert unavailable.status == "UNAVAILABLE" and "staged Python files" in unavailable.reason
    assert baseline.status == "PASSED" and baseline.check_id == "repository.patch"
    assert not validation.blocks_candidate([unavailable, baseline])
    (tmp_path / "--collect-only.py").touch()
    assert validation._targets(workspace, check) == ["./--collect-only.py"]


def test_validation_refuses_to_start_with_network_access():
    with pytest.raises(validation.RepositoryValidationError, match="required validation isolation"):
        validation._verify_isolation({"HostConfig": {"NetworkMode": "bridge"}, "Mounts": []})
