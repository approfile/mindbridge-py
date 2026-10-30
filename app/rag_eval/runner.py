from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import Base
from app.models.entities import KnowledgeChunk
from app.services.knowledge import KnowledgeService
from app.rag_eval.dataset import ROOT, load_dataset
from app.rag_eval.metrics import aggregate, score_ranking


def evaluate_case(service: KnowledgeService, case: dict, top_k: int, mode: str = 'bm25-rerank') -> dict:
    if 'qrels' not in case:
        raise ValueError('Explicit passage qrels are required')
    started = time.perf_counter()
    retrieved = service.retrieve(case['question'], top_k, mode=mode, expand=False)
    lookup = {row.id: f'{row.source}#{row.source_index}' for row in service.db.query(KnowledgeChunk).all()}
    keys = [lookup[item.chunk_id] for item in retrieved]
    qrels = {f"{item['source']}#{item['sourceIndex']}": item['gain'] for item in case['qrels']}
    metrics = score_ranking(keys, qrels, top_k)
    return {'id': case['id'], 'question': case['question'], **metrics,
            'retrieved': [{'rank': i + 1, 'key': key, 'score': item.score, 'gain': qrels.get(key, 0)}
                          for i, (key, item) in enumerate(zip(keys, retrieved))],
            'elapsedMs': round((time.perf_counter() - started) * 1000, 3)}


def evaluate_service(service: KnowledgeService, dataset: dict, mode: str) -> dict:
    if (service.settings.knowledge_chunk_size, service.settings.knowledge_chunk_overlap) != (
        dataset['chunking']['size'], dataset['chunking']['overlap']
    ):
        raise ValueError('Chunking differs from the frozen qrels corpus')
    results = [evaluate_case(service, case, service.settings.knowledge_top_k, mode) for case in dataset['queries']]
    return {**aggregate(results), 'topK': service.settings.knowledge_top_k, 'mode': mode,
            'datasetVersion': dataset['version'], 'datasetSha256': dataset['datasetSha256'],
            'frozenVerified': dataset['frozenVerified'], 'scoring': 'anchor-passage-qrels-v2',
            'meanQueryMs': sum(row['elapsedMs'] for row in results) / len(results),
            'results': results}


def evaluate(settings: Settings | None = None, mode: str = 'bm25-rerank') -> dict:
    settings = settings or get_settings()
    dataset = load_dataset(settings.rag_eval_dataset)
    with tempfile.TemporaryDirectory(prefix='mindbridge-rag-v2-') as temporary:
        values = settings.model_dump()
        values.update(database_url=f'sqlite:///{temporary}/evaluation.sqlite3',
                      knowledge_chunk_size=dataset['chunking']['size'], knowledge_chunk_overlap=dataset['chunking']['overlap'],
                      knowledge_vector_enabled=mode == 'hybrid', knowledge_vector_required=mode == 'hybrid',
                      knowledge_rerank_enabled=mode != 'bm25', chroma_persist_dir=f'{temporary}/chroma',
                      chroma_snapshot_dir=f'{temporary}/snapshots')
        isolated = Settings(_env_file=None, **values)
        engine = create_engine(isolated.database_url)
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as db:
                service = KnowledgeService(db, isolated)
                embedding_calls = [0]
                original = service.vector_store._embed
                def counted(texts):
                    embedding_calls[0] += 1
                    return original(texts)
                service.vector_store._embed = counted
                for source in sorted(dataset['sources']):
                    service.ensure_source(source, (ROOT/'app/knowledge'/source).read_text())
                indexing_calls = embedding_calls[0]
                report = evaluate_service(service, dataset, mode)
                report.update(createdAt=datetime.utcnow().isoformat(), annotation=dataset['annotation'],
                              embeddingModel=isolated.openai_embedding_model if mode == 'hybrid' else None,
                              indexingEmbeddingRequests=indexing_calls,
                              queryEmbeddingRequests=embedding_calls[0] - indexing_calls,
                              isolatedDatabase=True)
        finally:
            engine.dispose()
    output = Path(settings.rag_eval_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def check_regression(report: dict, baseline: dict, tolerance: float = 1e-10) -> None:
    for key in ['datasetSha256', 'mode', 'topK', 'scoring']:
        if report[key] != baseline[key]:
            raise ValueError('Cannot compare different evaluation contracts: ' + key)
    for key in ['recallAtK', 'precisionAtK', 'mrr', 'ndcgAtK', 'hitRate']:
        if report[key] + tolerance < baseline[key]:
            raise AssertionError(f'{key} regressed: {baseline[key]} -> {report[key]}')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['bm25', 'bm25-rerank', 'hybrid'], default='bm25-rerank')
    parser.add_argument('--baseline')
    args = parser.parse_args()
    report = evaluate(mode=args.mode)
    if args.baseline:
        check_regression(report, json.loads(Path(args.baseline).read_text()))
    print(json.dumps({key: value for key, value in report.items() if key != 'results'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
