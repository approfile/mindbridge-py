"""Widen structured trace storage on MySQL; preserve existing rows verbatim."""
from sqlalchemy import inspect, text


def upgrade_trace_storage(engine):
    if engine.dialect.name != 'mysql':
        return  # SQLite TEXT already supports this payload size.
    def complete(connection):
        column = next(c for c in inspect(connection).get_columns('agent_run_traces')
                      if c['name'] == 'agent_steps_json')
        return str(column['type']).upper() in {'MEDIUMTEXT', 'LONGTEXT'}
    with engine.connect() as connection:
        if complete(connection):
            return
    with engine.begin() as connection:
        locked = connection.execute(text("SELECT GET_LOCK('mindbridge:trace_storage_schema',30)")).scalar()
        if locked != 1:
            raise RuntimeError('Could not acquire trace storage migration lock')
        try:
            if not complete(connection):
                connection.exec_driver_sql('ALTER TABLE agent_run_traces MODIFY COLUMN agent_steps_json MEDIUMTEXT NOT NULL')
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('mindbridge:trace_storage_schema')"))
