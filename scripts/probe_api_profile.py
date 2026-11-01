"""Small paid BYOK protocol probe using existing deployment keys privately.

Creates only an in-memory SQL database and temporary Chroma directory. Does not
save credentials/profiles or modify business data. Exports metadata/usage only.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.core.database import Base
from app.services.model_profiles import ModelProfileService, ProfileInput
from app.services.model_gateway import usage_snapshot
from app.services.observability import observation_scope


def main():
    settings = get_settings().model_copy(deep=True)
    if not (settings.openai_api_key and settings.openai_embedding_api_key):
        raise SystemExit('Fill independent chat and embedding keys in .env first. Never pass keys on the command line.')
    if 'api.deepseek.com' not in settings.openai_base_url or 'dashscope.aliyuncs.com' not in settings.openai_embedding_base_url:
        raise SystemExit('This probe is specifically for the DeepSeek + mainland DashScope catalog.')
    output = Path('target/api-profile-probe') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output.mkdir(parents=True)
    report = {'passed':False, 'kind':'real-api-capability-probe', 'notice':'No business quality/performance claim.'}
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    try:
        with tempfile.TemporaryDirectory(prefix='mindbridge-api-profile-') as folder, Session(engine) as db:
            settings.chroma_persist_dir = str(Path(folder) / 'chroma')
            settings.chroma_snapshot_dir = str(Path(folder) / 'snapshots')
            incoming = ProfileInput(mode='api', chat={'connection':'deepseek','model':settings.openai_model,
                                                      'api_key':settings.openai_api_key},
                                    embedding={'connection':'dashscope-embedding','model':settings.openai_embedding_model,
                                               'api_key':settings.openai_embedding_api_key}, embedding_dimensions=1024)
            with observation_scope(ensure_run=True, reuse=False) as state:
                try:
                    _, _, report['capabilities'] = ModelProfileService(db, settings).test(0, incoming)
                    report['passed'] = True
                finally:
                    report['usage'] = usage_snapshot(state)
                    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                    print(f'Report: {output}/report.json', flush=True)
    finally:
        engine.dispose()
    print('PASS real BYOK non-stream / stream / JSON / reviewer / embedding contract')


if __name__ == '__main__':
    main()
