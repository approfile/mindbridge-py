"""Additive upgrade for pre-outbox databases; never removes historical jobs."""
from sqlalchemy import Engine, inspect, text


_COLUMNS = {
    "idempotency_key": "VARCHAR(128) NULL",
    "claim_token": "VARCHAR(64) NULL",
    "lease_expires_at": "DATETIME NULL",
    "correlation_json": "TEXT NULL",
}
_INDEX = "uq_tool_jobs_idempotency_key"


def upgrade_tool_jobs(engine: Engine) -> None:
    def complete(connection):
        inspector = inspect(connection)
        columns = {item["name"] for item in inspector.get_columns("tool_jobs")}
        indexes = {item["name"] for item in inspector.get_indexes("tool_jobs")}
        return set(_COLUMNS).issubset(columns) and _INDEX in indexes

    with engine.connect() as connection:
        if complete(connection):
            return

    with engine.begin() as connection:
        mysql = connection.dialect.name == "mysql"
        if mysql:
            locked = connection.execute(text("SELECT GET_LOCK('mindbridge:tool_jobs_schema', 30)")).scalar()
            if locked != 1:
                raise RuntimeError("Could not acquire tool job migration lock")
        elif connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            if complete(connection):
                return
            columns = {item["name"] for item in inspect(connection).get_columns("tool_jobs")}
            for name, definition in _COLUMNS.items():
                if name not in columns:
                    connection.exec_driver_sql(f"ALTER TABLE tool_jobs ADD COLUMN {name} {definition}")

            # Give one canonical job per legacy report/tool pair a key. Other
            # historical attempts remain intact and nullable, including DEAD jobs.
            rows = connection.execute(text(
                "SELECT id, report_id, kind, status, idempotency_key FROM tool_jobs ORDER BY id DESC"
            )).mappings().all()
            owned = {row["idempotency_key"] for row in rows if row["idempotency_key"]}
            priority = {"SUCCESS": 0, "RUNNING": 1, "PENDING": 1, "DEAD": 2}
            for row in sorted(rows, key=lambda row: priority.get(row["status"], 3)):
                key = f"{row['report_id']}:{row['kind']}"
                if key not in owned:
                    connection.execute(text(
                        "UPDATE tool_jobs SET idempotency_key = :key WHERE id = :id"
                    ), {"key": key, "id": row["id"]})
                    owned.add(key)
            indexes = {item["name"] for item in inspect(connection).get_indexes("tool_jobs")}
            if _INDEX not in indexes:
                connection.exec_driver_sql(f"CREATE UNIQUE INDEX {_INDEX} ON tool_jobs (idempotency_key)")
        finally:
            if mysql:
                connection.execute(text("SELECT RELEASE_LOCK('mindbridge:tool_jobs_schema')"))
