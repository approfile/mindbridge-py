"""Run preregistered isolated MySQL/Redis HTTP baseline (paid unless --mock)."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, help='Fresh, pre-created mindbridge_bench_* MySQL database')
    parser.add_argument('--output', default='target/system-benchmark/'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    parser.add_argument('--smoke', action='store_true', help='Two nominal scenarios, one concurrency, one trial, both variants + faults + overload')
    parser.add_argument('--mock', action='store_true', help='Harness plumbing only; no real-model quality claim')
    args = parser.parse_args()
    from app.benchmark.runner import run_benchmark
    run_benchmark(args)


if __name__ == '__main__':
    main()
