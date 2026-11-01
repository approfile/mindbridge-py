from __future__ import annotations

import shutil
import hashlib
import math
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from app.core.config import Settings
from app.models.entities import KnowledgeChunk
from app.services.observability import timed
from app.services.turn_budget import bounded_timeout, checkpoint
from app.services.model_gateway import ModelAttempt, embedding_stage


PRIMARY_RETRIEVAL_LABEL = "Chroma vector + BM25 hybrid + local reranker"
FALLBACK_RETRIEVAL_LABEL = "local BM25 + hybrid_score reranker"


class VectorStoreUnavailable(RuntimeError):
    pass


def bind_embedding_namespace(settings):
    """Legacy SQL vectors have no model identity; do not import them blindly."""
    result = settings.model_copy(deep=True)
    if result.knowledge_vector_enabled and not result.embedding_index_scope:
        identity = ['public-campus-kb-v1', 'deployment', result.openai_embedding_base_url,
                    result.openai_embedding_model, result.embedding_dimensions,
                    hashlib.sha256(result.openai_embedding_api_key.encode()).hexdigest(),
                    result.knowledge_chunk_size, result.knowledge_chunk_overlap, result.embedding_query_instruction]
        result.embedding_index_scope = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return result


@dataclass
class VectorSearchHit:
    chunk_id: int | None
    source: str
    source_index: int
    content: str
    score: float


class ChromaKnowledgeStore:
    """OpenAI-compatible cloud/selfhost embeddings stored and queried in Chroma."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.can_embed = False
        self.error = ""
        if not settings.knowledge_vector_enabled:
            self.error = "Chroma 向量库未启用"
            return
        if not (settings.openai_embedding_api_key or (not settings.embedding_index_scope and settings.openai_api_key)
                or settings.embedding_allow_no_key):
            if settings.knowledge_vector_required:
                raise VectorStoreUnavailable("缺少独立的 embedding 凭据，无法启用 Chroma 主检索方案")
            self.error = f"缺少独立的 embedding 凭据，Chroma 不可用，已回退到{FALLBACK_RETRIEVAL_LABEL}"
            return
        try:
            import chromadb
        except ImportError as exc:
            if settings.knowledge_vector_required:
                raise VectorStoreUnavailable("缺少 chromadb 依赖，无法启用 Chroma + text-embedding-3-small 主检索方案") from exc
            self.error = f"缺少 chromadb 依赖，Chroma + text-embedding-3-small 不可用，已回退到{FALLBACK_RETRIEVAL_LABEL}"
            return

        persist_dir = self._resolve_path(settings.chroma_persist_dir)
        persist_dir.mkdir(parents=True, exist_ok=True)
        self.persist_dir = persist_dir
        self.client = chromadb.PersistentClient(path=str(persist_dir))
        self.collection_name = settings.chroma_collection_name
        if settings.embedding_index_scope:
            identity = settings.embedding_index_scope + ':' + settings.embedding_corpus_version
            self.collection_name = 'mb-profile-' + hashlib.sha256(identity.encode()).hexdigest()[:48]
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=None,
            metadata={"hnsw:space": "cosine", "embedding_model": settings.openai_embedding_model},
        )
        self.can_embed = settings.knowledge_vector_enabled

    def cached_embeddings(self, chunks):
        if not chunks:
            return {}
        result = self.collection.get(ids=[self._id(c.id) for c in chunks], include=['embeddings', 'documents'])
        known = {self._id(c.id): c for c in chunks}
        cached = {}
        for index, identity in enumerate(result['ids']):
            chunk = known[identity]
            if result['documents'][index] != chunk.content:
                continue
            vector = result['embeddings'][index]
            if len(vector) and (not self.settings.embedding_dimensions or len(vector) == self.settings.embedding_dimensions):
                cached[chunk.id] = [float(v) for v in vector]
        return cached

    def upsert_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        rows = [chunk for chunk in chunks if chunk.id is not None and chunk.content.strip()]
        if not rows:
            return 0
        ids = [self._id(chunk.id) for chunk in rows]
        documents = [chunk.content for chunk in rows]
        metadatas = [
            {"db_id": int(chunk.id), "source": chunk.source, "source_index": int(chunk.source_index)}
            for chunk in rows
        ]
        self.collection.upsert(ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings)
        self.snapshot()
        return len(rows)

    def sync_chunks(self, chunks: list[KnowledgeChunk], embeddings: list[list[float]]) -> int:
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self.collection.get().get("ids", []))
        stale_ids = sorted(current_ids - valid_ids)
        if stale_ids:
            self.collection.delete(ids=stale_ids)
        return self.upsert_chunks(chunks, embeddings)

    def has_exact_chunk_ids(self, chunks: list[KnowledgeChunk]) -> bool:
        valid_ids = {self._id(int(chunk.id)) for chunk in chunks if chunk.id is not None}
        current_ids = set(self.collection.get().get("ids", []))
        return current_ids == valid_ids

    def delete_source(self, source: str) -> None:
        if not self.can_embed:
            return
        self.collection.delete(where={"source": source})

    @timed('retrieval.chroma_query')
    def query(self, query_embedding: list[float], top_k: int) -> list[VectorSearchHit]:
        result = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "metadatas", "distances"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        hits = []
        for index, document in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = float(distances[index]) if index < len(distances) else 1.0
            hits.append(
                VectorSearchHit(
                    chunk_id=int(metadata["db_id"]) if metadata.get("db_id") is not None else None,
                    source=str(metadata.get("source", "")),
                    source_index=int(metadata.get("source_index", 0)),
                    content=document or "",
                    score=1.0 / (1.0 + max(0.0, distance)),
                )
            )
        return hits

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not self.can_embed:
            raise VectorStoreUnavailable(self.error or "Chroma + text-embedding-3-small 主检索方案不可用")
        # 部分 embedding 服务有批量大小限制，按 20 条分片
        batch_size = 20
        if len(texts) <= batch_size:
            return self._embed(texts)
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            all_embeddings.extend(self._embed(batch))
        return all_embeddings

    def snapshot(self) -> str | None:
        if not self.can_embed:
            return None
        if not self.persist_dir.exists():
            return None
        snapshot_root = self._resolve_path(self.settings.chroma_snapshot_dir)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        destination = snapshot_root / datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")
        shutil.copytree(self.persist_dir, destination)
        self._prune_snapshots(snapshot_root)
        return str(destination)

    def count(self) -> int:
        if not self.can_embed:
            return 0
        return int(self.collection.count())

    @timed('embedding.request')
    def _embed(self, texts: list[str]) -> list[list[float]]:
        checkpoint()
        api_key = self.settings.openai_embedding_api_key
        if not self.settings.embedding_index_scope:
            api_key = api_key or self.settings.openai_api_key
        base_url = self.settings.openai_embedding_base_url or "https://api.openai.com/v1"
        payload = {
            "model": self.settings.openai_embedding_model,
            "input": [text if text.strip() else " " for text in texts],
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        with ModelAttempt(self.settings, provider='openai', base_url=base_url,
                          model=self.settings.openai_embedding_model, operation='embedding', stage=embedding_stage()) as attempt:
            response = httpx.post(
                f"{base_url}/embeddings", headers=headers, json=payload,
                timeout=bounded_timeout(self.settings.embedding_timeout_seconds),
            )
            attempt.response(response)
            response.raise_for_status()
            data = response.json()
            attempt.capture(data)
            checkpoint()
            rows = sorted(data.get("data", []), key=lambda item: item.get("index", 0))
            embeddings = [row.get("embedding") for row in rows]
            if ([row.get('index') for row in rows] != list(range(len(texts)))
                    or len(embeddings) != len(texts) or any(not embedding for embedding in embeddings)):
                raise VectorStoreUnavailable("OpenAI embeddings 接口返回向量数量不匹配")
            vectors = [[float(value) for value in embedding] for embedding in embeddings]
            if (any(not all(math.isfinite(v) for v in vector) or not any(v != 0 for v in vector) for vector in vectors)
                    or len({len(vector) for vector in vectors}) > 1
                    or (self.settings.embedding_dimensions and any(len(v) != self.settings.embedding_dimensions for v in vectors))):
                raise VectorStoreUnavailable('Embedding 向量维度或数值不合法')
            return vectors

    def _resolve_path(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.settings.project_root / path

    def _prune_snapshots(self, snapshot_root: Path) -> None:
        keep = max(1, self.settings.chroma_snapshot_keep)
        snapshots = sorted([path for path in snapshot_root.iterdir() if path.is_dir()], reverse=True)
        for stale in snapshots[keep:]:
            shutil.rmtree(stale, ignore_errors=True)

    def _id(self, chunk_id: int) -> str:
        return f"knowledge-chunk-{chunk_id}"


ChromaKnowledgeVectorStore = ChromaKnowledgeStore
