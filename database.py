from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import DATABASE_URL, logger

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def ensure_schema():
    """Легка міграція: додає відсутні колонки в наявні таблиці без Alembic."""
    try:
        insp = sa_inspect(engine)
        if "devices" in insp.get_table_names():
            cols = {c["name"] for c in insp.get_columns("devices")}
            if "client_id" not in cols:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE devices ADD COLUMN client_id VARCHAR DEFAULT ''"))
                logger.info("ensure_schema: додано колонку devices.client_id")
    except Exception as e:
        logger.warning("ensure_schema не вдалося: %s", e)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
