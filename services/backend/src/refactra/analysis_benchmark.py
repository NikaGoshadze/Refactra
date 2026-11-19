"""Durable operator benchmark core with local untrusted response artifacts."""

import json
import tomllib
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select

from refactra.analysis_context import FORMAT, PreparedAnalysisContext, save_analysis_context
from refactra.analysis_request import INSTRUCTIONS, BudgetExceeded
from refactra.db import sessions
from refactra.models import ModelReservation, Run
from refactra.openai_analysis import AnalysisProviderError, execute_read_only_analysis

SUITE_ROOT = Path(__file__).resolve().parents[2] / "benchmarks" / "analysis"
EXECUTION_LOCK = 72341003
CAP_USD = Decimal("1")


def _write(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def run_benchmark(
    engine, providers, output_root: Path, *, suite_root=SUITE_ROOT, trial_id: str | None = None
) -> Path:
    """Use an already-migrated, dedicated PostgreSQL schema, never the app's ledger.

    Only synthetic fixtures are loaded. Outputs are untrusted local artifacts; they
    are never executed. Keep output_root ignored. No automatic retry or grading.
    """
    if set(providers) != {"openai", "deepseek"}:
        raise ValueError("Both benchmark providers are required.")
    raw = (suite_root / "cases.toml").read_bytes()
    grading = (suite_root / "grading.toml").read_bytes()
    profiles = {name: asdict(provider.pricing) for name, provider in providers.items()}
    configuration = {
        "format": "analysis-benchmark-v1",
        "instructions": INSTRUCTIONS,
        "profiles": profiles,
    }
    # Preserve the original fingerprint when no new trial is explicitly requested.
    if trial_id is not None:
        if (
            not 1 <= len(trial_id) <= 40
            or not trial_id.isascii()
            or not all(char.isalnum() or char in "-_" for char in trial_id)
        ):
            raise ValueError(
                "Trial labels require 1-40 ASCII letters, digits, hyphens or underscores."
            )
        configuration["trial_id"] = trial_id
    encoded_configuration = json.dumps(configuration, sort_keys=True, default=str).encode()
    suite_hash = sha256(raw + b"\0" + grading + b"\0" + encoded_configuration).hexdigest()
    cases = tomllib.loads(raw.decode())["cases"]
    if len(cases) != 5 or len({case["id"] for case in cases}) != 5:
        raise ValueError("Five distinct benchmark cases are required.")
    output = output_root / suite_hash
    factory = sessions(engine)
    # Hold a dedicated session-level lock, not a transaction, across provider calls.
    with engine.connect() as owner:
        if owner.scalar(select(func.current_schema())) == "public":
            raise ValueError("Use a dedicated benchmark schema, not public.")
        if not owner.scalar(select(func.pg_try_advisory_lock(EXECUTION_LOCK))):
            raise ValueError("Another benchmark runner is active.")
        owner.commit()
        try:
            output.mkdir(parents=True, exist_ok=True)
            manifest = {
                "suite_hash": suite_hash,
                "trial_id": trial_id,
                "cases_sha256": sha256(raw).hexdigest(),
                "grading_sha256": sha256(grading).hexdigest(),
                "profiles": profiles,
                "cap_usd": str(CAP_USD),
            }
            _write(output / "manifest.json", manifest)
            for repeat in range(3):
                for index, case in enumerate(cases):
                    run_id = uuid5(NAMESPACE_URL, f"{suite_hash}/{case['id']}/{repeat}")
                    files = [
                        {
                            "path": path,
                            "text": "\n".join(
                                f"{line}: {text}"
                                for line, text in enumerate(source.splitlines(), 1)
                            ),
                        }
                        for path, source in sorted(case["files"].items())
                    ]
                    payload = {
                        "schema_version": FORMAT,
                        "run_id": str(run_id),
                        "repository_id": 1,
                        "base_sha": sha256(json.dumps(files).encode()).hexdigest()[:40],
                        "task_text": case["task"],
                        "files": files,
                    }
                    with factory.begin() as session:
                        if session.get(Run, run_id) is None:
                            # Synthetic IDs are confined to the dedicated benchmark schema.
                            session.add(
                                Run(
                                    id=run_id,
                                    requested_by=1,
                                    github_installation_id=1,
                                    github_repository_id=1,
                                    base_branch="fixture",
                                    base_sha=payload["base_sha"],
                                    task_text=case["task"],
                                    status="CREATED",
                                    workflow_version="repository-v0.1",
                                )
                            )
                            session.flush()
                        document = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
                        save_analysis_context(session, PreparedAnalysisContext(run_id, document))
                    names = ["openai", "deepseek"]
                    if (repeat + index) % 2:
                        names.reverse()
                    for name in names:
                        attempt = uuid5(run_id, name)
                        path = output / f"{attempt}.json"
                        if path.exists():
                            saved = json.loads(path.read_text(encoding="utf-8"))
                            if saved["attempt_id"] != str(attempt):
                                raise ValueError("Unexpected benchmark result artifact.")
                            continue
                        with factory() as session:
                            existing = session.get(ModelReservation, attempt) is not None
                        result, status, started = None, "unknown_result", perf_counter()
                        started_at = datetime.now(UTC).isoformat()
                        if not existing:
                            try:
                                result = execute_read_only_analysis(
                                    factory,
                                    run_id,
                                    attempt,
                                    providers[name],
                                    ledger_limit_usd=CAP_USD,
                                )
                                status = "completed"
                            except AnalysisProviderError:
                                status = "provider_failed"
                            except BudgetExceeded:
                                status = "budget_blocked"
                        elapsed = perf_counter() - started if not existing else None
                        with factory() as session:
                            row = session.get(ModelReservation, attempt)
                            record = {
                                "attempt_id": str(attempt),
                                "run_id": str(run_id),
                                "case_id": case["id"],
                                "repeat": repeat + 1,
                                "provider": name,
                                "model": providers[name].pricing.model,
                                "status": status,
                                "started_at": started_at if not existing else None,
                                "elapsed_seconds": elapsed,
                                "prompt_sha256": sha256(
                                    INSTRUCTIONS.encode() + document
                                ).hexdigest(),
                                "reserved_usd": str(row.maximum_cost_usd) if row else None,
                                "estimated_cost_usd": str(row.actual_cost_usd)
                                if row and row.actual_cost_usd is not None
                                else None,
                                "input_tokens": row.actual_input_tokens if row else None,
                                "output_tokens": row.actual_output_tokens if row else None,
                                "cached_input_tokens": result.cached_input_tokens
                                if result
                                else None,
                                "response_id": result.response_id if result else None,
                                "text": result.text if result else None,
                            }
                        _write(path, record)
                        if status == "budget_blocked":
                            return output
            return output
        finally:
            owner.execute(select(func.pg_advisory_unlock(EXECUTION_LOCK)))
            owner.commit()
