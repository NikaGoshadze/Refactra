"""Summarize local metadata, never execute or print model response text."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from statistics import median
from uuid import UUID


def summarize(output: Path) -> dict:
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if set(manifest["profiles"]) != {"openai", "deepseek"}:
        raise ValueError("Both benchmark profiles are required.")
    rows = []
    for path in sorted(output.glob("*.json")):
        if path.name in {"manifest.json", "summary.json"}:
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        if str(UUID(row["attempt_id"])) != path.stem or row["provider"] not in manifest["profiles"]:
            raise ValueError("Unexpected benchmark artifact.")
        rows.append(row)
    report = {
        "suite_hash": manifest["suite_hash"],
        "trial_id": manifest.get("trial_id"),
        "planned_requests": 30,
        "recorded_requests": len(rows),
        "cost_scope": ("This directory only; PostgreSQL may hold additional interrupted attempts."),
        "quality_score": None,
        "providers": {},
    }
    for name, profile in manifest["profiles"].items():
        selected = [row for row in rows if row["provider"] == name]
        if len(selected) > 15:
            raise ValueError("Too many benchmark result artifacts.")
        statuses = Counter(row["status"] for row in selected)
        timed = [
            row["elapsed_seconds"]
            for row in selected
            if row["status"] in {"completed", "provider_failed"}
            and row["elapsed_seconds"] is not None
        ]
        settled = sum(
            (
                Decimal(row["estimated_cost_usd"])
                for row in selected
                if row["estimated_cost_usd"] is not None
            ),
            Decimal(0),
        )
        held = sum(
            (
                Decimal(row["reserved_usd"])
                for row in selected
                if row["estimated_cost_usd"] is None and row["reserved_usd"] is not None
            ),
            Decimal(0),
        )
        report["providers"][name] = {
            "model": profile["model"],
            "statuses": dict(statuses),
            "missing_results": 15 - len(selected),
            "completion_rate": statuses["completed"] / 15,
            "confirmed_input_tokens": sum(row["input_tokens"] or 0 for row in selected),
            "confirmed_output_tokens": sum(row["output_tokens"] or 0 for row in selected),
            "reported_cached_tokens": sum(row["cached_input_tokens"] or 0 for row in selected),
            "cache_metadata_records": sum(
                row["cached_input_tokens"] is not None for row in selected
            ),
            "usage_unknown_records": sum(
                row["reserved_usd"] is not None and row["estimated_cost_usd"] is None
                for row in selected
            ),
            "recorded_settled_estimate_usd": str(settled),
            "recorded_holds_usd": str(held),
            "median_attempt_seconds": median(timed) if timed else None,
            "worst_attempt_seconds": max(timed) if timed else None,
        }
    return report
