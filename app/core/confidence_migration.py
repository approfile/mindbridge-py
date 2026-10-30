"""Allow absent confidence and add provenance without changing old values."""
from __future__ import annotations

import re

from sqlalchemy import inspect, text


def _complete(connection):
    columns = {c['name']: c for c in inspect(connection).get_columns('psychological_reports')}
    return columns['confidence']['nullable'] and 'confidence_metadata_json' in columns


def upgrade_confidence_storage(engine):
    with engine.connect() as connection:
        if _complete(connection):
            return
    if engine.dialect.name == 'sqlite':
        return _sqlite_upgrade(engine)
    if engine.dialect.name != 'mysql':
        raise RuntimeError('Confidence migration supports SQLite and MySQL')
    with engine.begin() as connection:
        if connection.execute(text("SELECT GET_LOCK('mindbridge:confidence_schema',30)")).scalar() != 1:
            raise RuntimeError('Could not acquire confidence migration lock')
        try:
            columns = {c['name']: c for c in inspect(connection).get_columns('psychological_reports')}
            if not columns['confidence']['nullable']:
                connection.exec_driver_sql('ALTER TABLE psychological_reports MODIFY COLUMN confidence FLOAT NULL')
            if 'confidence_metadata_json' not in columns:
                connection.exec_driver_sql('ALTER TABLE psychological_reports ADD COLUMN confidence_metadata_json TEXT NULL')
        finally:
            connection.execute(text("SELECT RELEASE_LOCK('mindbridge:confidence_schema')"))


def _sqlite_upgrade(engine):
    # SQLite cannot DROP NOT NULL directly. Rebuild just this table under a
    # writer lock, restoring its explicit indexes/triggers and FK setting.
    with engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql('PRAGMA foreign_keys').scalar()
        connection.commit()
        connection.exec_driver_sql('PRAGMA foreign_keys=OFF'); connection.commit()
        try:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            if not _complete(connection):
                columns = {c['name']: c for c in inspect(connection).get_columns('psychological_reports')}
                if not columns['confidence']['nullable']:
                    ddl = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='psychological_reports'").scalar_one()
                    ddl, replacements = re.subn(r'(?i)(\bconfidence\b\s+(?:FLOAT|REAL|DOUBLE(?:\s+PRECISION)?|NUMERIC)(?:\([^)]*\))?)\s+NOT\s+NULL\b', r'\1', ddl)
                    if replacements != 1:
                        raise RuntimeError('Unsupported legacy SQLite confidence column declaration')
                    ddl, count = re.subn(r'(?i)^(CREATE\s+TABLE\s+)(?:"psychological_reports"|`psychological_reports`|psychological_reports)',
                                         r'\1"_confidence_reports_upgrade"', ddl, count=1)
                    if count != 1:
                        raise RuntimeError('Unsupported legacy SQLite report table declaration')
                    objects = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE tbl_name='psychological_reports' AND type IN ('index','trigger') AND sql IS NOT NULL").scalars().all()
                    violations = set(connection.exec_driver_sql('PRAGMA foreign_key_check').all())
                    sequence = None
                    if connection.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name='sqlite_sequence'").scalar():
                        sequence = connection.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name='psychological_reports'").scalar()
                    connection.exec_driver_sql(ddl)
                    names = ','.join('"' + name.replace('"', '""') + '"' for name in columns)
                    connection.exec_driver_sql(f'INSERT INTO _confidence_reports_upgrade ({names}) SELECT {names} FROM psychological_reports')
                    connection.exec_driver_sql('DROP TABLE psychological_reports')
                    connection.exec_driver_sql('ALTER TABLE _confidence_reports_upgrade RENAME TO psychological_reports')
                    for sql in objects:
                        connection.exec_driver_sql(sql)
                    if sequence is not None:
                        connection.exec_driver_sql("UPDATE sqlite_sequence SET seq=? WHERE name='psychological_reports'", (sequence,))
                    if set(connection.exec_driver_sql('PRAGMA foreign_key_check').all()) != violations:
                        raise RuntimeError('Confidence migration changed SQLite foreign key integrity')
                if 'confidence_metadata_json' not in columns:
                    connection.exec_driver_sql('ALTER TABLE psychological_reports ADD COLUMN confidence_metadata_json TEXT NULL')
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql(f'PRAGMA foreign_keys={int(bool(foreign_keys))}')
            connection.commit()
