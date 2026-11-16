import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    enable_mock: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv(
                "REFACTRA_DATABASE_URL",
                "postgresql+psycopg://refactra:refactra_local@localhost:5433/refactra",
            ),
            enable_mock=os.getenv("REFACTRA_ENABLE_MOCK", "false").lower() == "true",
        )
