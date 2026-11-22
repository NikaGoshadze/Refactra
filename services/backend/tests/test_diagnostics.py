import json
import logging

import pytest

from refactra.models import Run, RunEvent
from refactra.openai_analysis import (
    AnalysisModelInput,
    AnalysisProviderError,
    OpenAIAnalysisProvider,
)

from .test_openai_analysis import response


def test_workflow_logs_only_committed_metadata(session_factory, caplog):
    caplog.set_level(logging.INFO, logger="refactra.workflow")
    with session_factory.begin() as session:
        run = Run(task_text="private prompt")
        session.add(run)
        session.flush()
        session.add(
            RunEvent(
                run_id=run.id,
                event_type="CANDIDATE_GENERATION_FAILED",
                payload={"code": "fixture_failure", "detail": "private source"},
            )
        )
    with pytest.raises(RuntimeError), session_factory.begin() as session:
        session.add(RunEvent(run_id=run.id, event_type="NOT_COMMITTED", payload={}))
        session.flush()
        raise RuntimeError

    (logged,) = [json.loads(item.message) for item in caplog.records]
    assert logged["run_id"] == str(run.id)
    assert logged["stage_event"] == "CANDIDATE_GENERATION_FAILED"
    assert logged["code"] == "fixture_failure"
    assert "private" not in caplog.text


def test_provider_reports_incomplete_reason_without_recording_content(monkeypatch):
    payload = response()
    payload["status"] = "incomplete"
    payload["incomplete_details"] = {"reason": "max_output_tokens"}
    monkeypatch.setattr(OpenAIAnalysisProvider, "_post", lambda *_: payload)
    provider = OpenAIAnalysisProvider("fixture")
    with pytest.raises(AnalysisProviderError) as failed:
        provider._generate(AnalysisModelInput(provider.pricing.model, "fixture", "{}", 100))
    assert failed.value.reason == "max_output_tokens"
    assert failed.value.usage is not None
