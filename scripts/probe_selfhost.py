"""Run the exact app capability contract against the configured selfhost services.

No cloud credentials, account/profile writes or knowledge indexing. Run inside
the local app container after SSH tunneling. Does incur GPU inference time.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings
from app.services.model_profiles import ModelProfileService, ProfileInput, fingerprint, probe_capabilities
from app.services.observability import observation_scope
from app.services.model_gateway import usage_snapshot


def main():
    settings = get_settings()
    if not (settings.selfhost_chat_base_url and settings.selfhost_embedding_base_url):
        raise SystemExit('Configure both selfhost endpoints and the SSH tunnel first.')
    from app.services.model_profiles import connections
    configured = connections(settings)
    data = {'mode':'selfhost', 'embedding_dimensions':1024}
    for role in ('chat','embedding'):
        item = configured[f'selfhost-{role}']
        data[role] = {'connection':item.id, 'model':item.model, 'key':'',
                      'binding':fingerprint([item.id,item.base_url,item.revision,item.model,fingerprint('')])}
    snapshot = ModelProfileService(None, settings).settings_for(0, data, 0)
    output = Path('target/selfhost-probe') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output.mkdir(parents=True)
    report = {'passed':False, 'kind':'real-selfhost-capability-probe',
              'modelRevision':settings.selfhost_model_revision,
              'notice':'Protocol validation only. Full business/quality/concurrency benchmark is a separate step.'}
    with observation_scope(ensure_run=True, reuse=False) as state:
        try:
            report['capabilities'] = probe_capabilities(snapshot)
            report['passed'] = True
        finally:
            report['usage'] = usage_snapshot(state)
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'Report: {output}/report.json')
    print('PASS real selfhost non-stream / stream / JSON / reviewer / context declaration / 1024-dimension embedding')


if __name__ == '__main__':
    main()
