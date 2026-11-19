"""Explicit local operator entry point; loading the module never makes paid calls."""

import argparse
import json
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.sql import func

from refactra.analysis_benchmark import run_benchmark
from refactra.benchmark_summary import summarize
from refactra.deepseek_analysis import DeepSeekAnalysisProvider
from refactra.openai_analysis import OpenAIAnalysisProvider

SCHEMA = "refactra_benchmark"
SETUP_LOCK = 72341004
KEYS = (
    "REFACTRA_DATABASE_URL",
    "REFACTRA_OPENAI_API_KEY",
    "REFACTRA_DEEPSEEK_API_KEY",
    "REFACTRA_DEEPSEEK_TOKENIZER_PATH",
)


def load_settings(path: Path) -> dict:
    settings = {"REFACTRA_DEEPSEEK_TOKENIZER_PATH": ".runtime/deepseek-v41-tokenizer.json"}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() in KEYS:
                settings[key.strip()] = value.strip().strip("\"'")
    settings.update({key: os.environ[key] for key in KEYS if key in os.environ})
    if any(not settings.get(key, "").strip() for key in KEYS):
        raise ValueError("Benchmark configuration is incomplete.")
    return settings


def configured_providers(settings):
    return {
        "openai": OpenAIAnalysisProvider(settings["REFACTRA_OPENAI_API_KEY"]),
        "deepseek": DeepSeekAnalysisProvider(
            settings["REFACTRA_DEEPSEEK_API_KEY"],
            tokenizer_path=Path(settings["REFACTRA_DEEPSEEK_TOKENIZER_PATH"]),
        ),
    }


def execute(settings, output_root: Path, *, trial_id=None) -> Path:
    url = make_url(settings["REFACTRA_DATABASE_URL"])
    if url.drivername != "postgresql+psycopg" or url.host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("A local PostgreSQL database is required.")
    providers = configured_providers(settings)
    engine = create_engine(
        url,
        pool_pre_ping=True,
        connect_args={
            "options": f"-csearch_path={SCHEMA}",
            "connect_timeout": 5,
        },
    )
    try:
        with engine.begin() as connection:
            connection.execute(select(func.pg_advisory_xact_lock(SETUP_LOCK)))
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))
            config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        return run_benchmark(engine, providers, output_root, trial_id=trial_id)
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Local read-only model benchmark operator")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--run", action="store_true", help="Make/resume paid calls under the $1 ledger cap"
    )
    mode.add_argument("--summary", type=Path, help="Summarize a result directory without API calls")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--trial", help="Explicit new trial label; preserves prior results and costs"
    )
    args = parser.parse_args(argv)
    if args.trial is not None and not args.run:
        parser.error("--trial requires --run")
    try:
        output = args.summary or execute(
            load_settings(args.env_file), Path(".runtime/benchmarks"), trial_id=args.trial
        )
        report = summarize(output)
        (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), "summary": report}, indent=2))
        return int(
            args.run and any(item["completion_rate"] != 1 for item in report["providers"].values())
        )
    except KeyboardInterrupt:
        print(
            "Benchmark interrupted; receipts remain recorded. No automatic retry.", file=sys.stderr
        )
        return 130
    except Exception as error:
        print(
            f"Benchmark command failed ({type(error).__name__}); "
            "check local configuration or result files. Credentials and responses suppressed.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
