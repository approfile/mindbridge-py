"""Idempotent, value-preserving legacy plaintext encryption migration.

Run with old writers stopped. MySQL advisory lock serializes new startup/MCP
processes. DDL may commit independently; the data pass is restartable.
"""
from sqlalchemy import inspect, text

from app.core.privacy_crypto import PREFIX, SensitiveText, cipher, decrypt_text, encrypt_text, key_path, PrivacyStorageError


def sensitive_columns():
    from app.core.database import Base
    from app.models import entities  # register all tables
    return {table.name: [column.name for column in table.columns if isinstance(column.type, SensitiveText)]
            for table in Base.metadata.sorted_tables
            if any(isinstance(column.type, SensitiveText) for column in table.columns)}


def upgrade_sensitive_storage(engine, settings=None):
    with engine.connect() as connection:
        if engine.dialect.name == 'mysql':
            if connection.execute(text("SELECT GET_LOCK('mindbridge:sensitive_storage',30)")).scalar() != 1:
                raise PrivacyStorageError('Sensitive migration lock unavailable')
            connection.commit()
        try:
            columns = sensitive_columns()
            state = connection.execute(text('SELECT key_id FROM privacy_migration_state WHERE id=1')).scalar()
            if state:
                if cipher(settings, create=False)[1] != state:
                    raise PrivacyStorageError('Sensitive storage key does not match database')
                return {'alreadyComplete': True, 'encryptedValues': 0}
            # Never manufacture a replacement key for an already encrypted DB.
            if not key_path(settings).exists():
                for table, names in columns.items():
                    for name in names:
                        if connection.execute(text(f'SELECT id FROM {table} WHERE {name} LIKE :prefix LIMIT 1'), {'prefix': PREFIX+'%'}).first():
                            raise PrivacyStorageError('Existing encrypted data requires its original key')
            key_id = cipher(settings)[1]
            if engine.dialect.name == 'mysql':
                for table, names in columns.items():
                    actual = {c['name']: c for c in inspect(connection).get_columns(table)}
                    for name in names:
                        column = actual[name]
                        if str(column['type']).upper() not in {'MEDIUMTEXT','LONGTEXT'}:
                            nullable = 'NULL' if column['nullable'] else 'NOT NULL'
                            connection.exec_driver_sql(f'ALTER TABLE {table} MODIFY COLUMN {name} MEDIUMTEXT {nullable}')
                connection.commit()
            elif engine.dialect.name != 'sqlite':
                raise PrivacyStorageError('Sensitive migration supports SQLite and MySQL')
            count = 0
            for table, names in columns.items():
                cursor = 0
                while True:
                    rows = connection.execute(text(f"SELECT id,{','.join(names)} FROM {table} WHERE id>:cursor ORDER BY id LIMIT 100"), {'cursor':cursor}).mappings().all()
                    if not rows:break
                    for row in rows:
                        updates = {}
                        for name in names:
                            value = row[name]
                            if value is None:continue
                            if value.startswith(PREFIX):
                                decrypt_text(value, settings)  # wrong/corrupt token aborts, never silently overwritten
                            else:
                                updates[name] = encrypt_text(value, settings)
                        if updates:
                            assignments = ','.join(f'{name}=:{name}' for name in updates)
                            connection.execute(text(f'UPDATE {table} SET {assignments} WHERE id=:row_id'), {**updates,'row_id':row['id']})
                            count += len(updates)
                    cursor = rows[-1]['id']
            connection.execute(text('INSERT INTO privacy_migration_state(id,key_id) VALUES(1,:key_id)'), {'key_id':key_id})
            connection.commit()
            return {'alreadyComplete': False, 'encryptedValues': count}
        except BaseException:
            connection.rollback(); raise
        finally:
            if engine.dialect.name == 'mysql':
                connection.execute(text("SELECT RELEASE_LOCK('mindbridge:sensitive_storage')"));connection.commit()
