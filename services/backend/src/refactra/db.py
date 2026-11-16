from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("Refactra requires PostgreSQL with the psycopg driver.")
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})


def sessions(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
