import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select

from refactra import analysis_benchmark as benchmark
from refactra.analysis_request import BudgetExceeded
from refactra.deepseek_analysis import PRICING as DEEPSEEK
from refactra.model_reservations import reserve_analysis_request
from refactra.models import ModelReservation
from refactra.openai_analysis import PRICING as OPENAI
from refactra.openai_analysis import AnalysisProviderError, AnalysisResult

from .test_model_reservations import prepare


class FakeProvider:
    def __init__(self, pricing, calls, *, fail=False):
        self.pricing, self.calls, self.inputs, self.fail = pricing, calls, [], fail

    def count_input_tokens(self, request):
        self.inputs.append(request)
        return 100

    def _generate(self, request):
        self.calls.append(request.model)
        if self.fail:
            raise AnalysisProviderError
        return AnalysisResult("resp_fixture", "Untrusted model answer", (100, 20), 10)


def providers(calls, *, fail=False):
    return {
        "openai": FakeProvider(OPENAI, calls, fail=fail),
        "deepseek": FakeProvider(DEEPSEEK, calls),
    }


def records(output):
    return [
        json.loads(path.read_text())
        for path in output.glob("*.json")
        if path.name != "manifest.json"
    ]


def test_thirty_paired_requests_exclude_grading_and_persist_usage(database, tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    for filename in ("cases.toml", "grading.toml"):
        (suite / filename).write_bytes((benchmark.SUITE_ROOT / filename).read_bytes())
    with (suite / "grading.toml").open("a") as stream:
        stream.write("\n# NEVER_SEND_GRADING_SENTINEL\n")
    calls = []
    adapters = providers(calls)
    output = benchmark.run_benchmark(database, adapters, tmp_path / "results", suite_root=suite)
    saved = records(output)
    assert len(saved) == len(calls) == 30
    assert calls[:4] == [OPENAI.model, DEEPSEEK.model, DEEPSEEK.model, OPENAI.model]
    assert all(row["status"] == "completed" and row["cached_input_tokens"] == 10 for row in saved)
    assert all(row["elapsed_seconds"] >= 0 and row["estimated_cost_usd"] for row in saved)
    assert {row["repeat"] for row in saved} == {1, 2, 3}
    for left, right in zip(adapters["openai"].inputs, adapters["deepseek"].inputs, strict=True):
        assert left.instructions == right.instructions and left.context_json == right.context_json
        assert "NEVER_SEND_GRADING_SENTINEL" not in left.context_json
        assert "1: " in left.context_json
    assert len({row["prompt_sha256"] for row in saved}) == 15
    benchmark.run_benchmark(database, adapters, tmp_path / "results", suite_root=suite)
    assert len(calls) == 30


def test_missing_result_after_paid_attempt_never_resends(database, tmp_path):
    calls = []
    adapters = providers(calls, fail=True)
    output = benchmark.run_benchmark(database, adapters, tmp_path)
    assert sum(row["status"] == "provider_failed" for row in records(output)) == 15
    lost = next(
        path
        for path in output.glob("*.json")
        if path.name != "manifest.json" and json.loads(path.read_text())["status"] == "completed"
    )
    lost.unlink()
    benchmark.run_benchmark(database, adapters, tmp_path)
    recovered = json.loads(lost.read_text())
    assert recovered["status"] == "unknown_result" and recovered["text"] is None
    assert recovered["started_at"] is None and recovered["elapsed_seconds"] is None
    assert len(calls) == 30


def test_cap_blocks_generation_and_execution_lock_prevents_overlap(database, tmp_path, monkeypatch):
    calls = []
    public = create_engine(database.url, connect_args={"options": "-csearch_path=public"})
    try:
        with pytest.raises(ValueError, match="dedicated benchmark schema"):
            benchmark.run_benchmark(public, providers(calls), tmp_path)
    finally:
        public.dispose()
    with database.connect() as owner:
        owner.execute(select(func.pg_advisory_lock(benchmark.EXECUTION_LOCK)))
        owner.commit()
        try:
            with pytest.raises(ValueError, match="runner is active"):
                benchmark.run_benchmark(database, providers(calls), tmp_path)
        finally:
            owner.execute(select(func.pg_advisory_unlock(benchmark.EXECUTION_LOCK)))
            owner.commit()
    monkeypatch.setattr(benchmark, "CAP_USD", Decimal("0.01"))
    output = benchmark.run_benchmark(database, providers(calls), tmp_path)
    assert not calls and [row["status"] for row in records(output)] == ["budget_blocked"]


def test_competing_admissions_cannot_exceed_the_benchmark_ledger_cap(session_factory):
    requests = [prepare(session_factory, expensive=True) for _ in range(2)]

    def admit(request):
        try:
            with session_factory.begin() as session:
                reserve_analysis_request(session, uuid4(), request, ledger_limit_usd=Decimal("1"))
            return "reserved"
        except BudgetExceeded as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(admit, requests)) == ["benchmark_cost", "reserved"]
    with session_factory() as session:
        assert session.scalar(select(func.sum(ModelReservation.maximum_cost_usd))) == Decimal(
            "0.75"
        )
