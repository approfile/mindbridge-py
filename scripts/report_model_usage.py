"""Export metadata-only usage: python scripts/report_model_usage.py --run-id ID --output target/usage"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.database import SessionLocal
from app.services.usage_reporting import attempts_csv, usage_report, usage_run_detail


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--scope', choices=['online_chat', 'knowledge_index', 'http_model_operation', 'all'], default='online_chat')
    parser.add_argument('--run-id')
    parser.add_argument('--output', default='target/model-usage')
    args = parser.parse_args()
    if not 1 <= args.hours <= 720:
        parser.error('--hours must be between 1 and 720')
    with SessionLocal() as db:
        report = usage_run_detail(db, args.run_id) if args.run_id else usage_report(db, args.hours, args.scope)
    if report is None:
        parser.error('run not found')
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.with_suffix('.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.run_id:
        path.with_suffix('.csv').write_text(attempts_csv(report['attempts']), encoding='utf-8-sig')
    print(f"Exported {path.with_suffix('.json')}" + (' and attempt CSV' if args.run_id else ''))


if __name__ == '__main__':
    main()
