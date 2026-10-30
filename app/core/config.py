from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    privacy_key_file: str = 'data/privacy.key'
    privacy_trace_retention_days: int = Field(default=30, ge=1, le=3650)
    privacy_content_retention_days: int = Field(default=90, ge=1, le=3650)
    privacy_access_audit_retention_days: int = Field(default=365, ge=30, le=3650)
    turn_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    turn_max_inflight: int = Field(default=4, ge=1, le=32)
    turn_max_inflight_per_user: int = Field(default=2, ge=1, le=32)
    turn_io_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    turn_cleanup_wait_seconds: float = Field(default=.2, ge=0, le=1)
    turn_max_request_bytes: int = Field(default=65536, ge=1024, le=1048576)
    database_pool_timeout_seconds: float = Field(default=5., gt=0)
    database_socket_timeout_seconds: int = Field(default=10, ge=1, le=60)
    observability_log_file: str = ""
    observability_log_max_bytes: int = Field(default=10_485_760, ge=1024)
    observability_log_backups: int = Field(default=3, ge=1, le=20)
    agent_framework: str = "event_driven_multi_agent"
    agent_max_rounds: int = 8
    agent_max_claims_per_round: int = 4
    agent_max_claims_per_agent: int = 3
    agent_final_acceptance_min_confidence: float | None = Field(default=None, exclude=True, description="Retired; ignored with warning. Plan acceptance uses plan-contract-v1.")
    agent_model_default_provider: str = ""
    agent_model_default_model: str = ""
    agent_model_coordinator_provider: str = ""
    agent_model_coordinator_model: str = ""
    agent_model_understanding_provider: str = ""
    agent_model_understanding_model: str = ""
    agent_model_safety_provider: str = ""
    agent_model_safety_model: str = ""
    agent_model_context_provider: str = ""
    agent_model_context_model: str = ""
    agent_model_response_provider: str = ""
    agent_model_response_model: str = ""
    ai_provider: str = "ollama"
    ai_temperature: float = 0.35
    ai_max_tokens: int = 512
    intent_json_mode: bool = True
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_name: str = "mindbridge-qwen2.5-7b-ft:latest"
    finetuned_model_dir: str = "models/mindbridge-qwen2.5-7b-ft"
    finetuned_model_file: str = "mindbridge-qwen2.5-7b-ft-q4_k_m.gguf"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_thinking_mode: Literal["auto", "enabled", "disabled"] = "auto"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_base_url: str = "https://api.openai.com/v1"
    openai_embedding_api_key: str = ""
    openai_embedding_base_url: str = "https://api.openai.com/v1"
    openai_embedding_api_key: str = ""
    database_url: str = "mysql+pymysql://mindbridge:mindbridge@127.0.0.1:3306/mindbridge?charset=utf8mb4"
    chat_history_limit: int = 10
    knowledge_top_k: int = 4
    knowledge_candidate_k: int = 16
    knowledge_chunk_size: int = 512
    knowledge_chunk_overlap: int = 64
    knowledge_hybrid_vector_weight: float = 0.65
    knowledge_hybrid_bm25_weight: float = 0.35
    knowledge_rerank_enabled: bool = True
    knowledge_vector_enabled: bool = True
    knowledge_vector_required: bool = False
    chroma_persist_dir: str = "data/chroma"
    chroma_collection_name: str = "mindbridge_knowledge"
    chroma_snapshot_dir: str = "data/chroma-snapshots"
    chroma_snapshot_keep: int = 5
    embedding_timeout_seconds: float = 30.0
    rag_eval_dataset: str = "app/rag_eval/data/retrieval-v1.json"
    rag_eval_output: str = "target/rag-eval-report.json"
    rag_eval_enabled: bool = False
    rag_eval_exit_after_run: bool = False
    excel_path: str = "data/mindbridge-risk-ledger.xlsx"
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_memory_ttl_seconds: int = 86400
    redis_memory_max_messages: int = 40
    redis_socket_timeout_seconds: float = 2.0
    memory_compaction_enabled: bool = True
    memory_compaction_recent_messages: int = 8
    memory_summary_max_chars: int = 500
    reply_max_revisions: int = Field(default=1, ge=0, le=1)
    reply_timeout_seconds: float = Field(default=45.0, gt=0)
    reply_max_chars: int = Field(default=8000, ge=100)
    reply_review_json_mode: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    smtp_timeout_seconds: float = 10.0
    alert_email_delivery_mode: str = "log"
    alert_email_from: str = ""
    alert_email_to: str = ""
    alert_email_subject_prefix: str = "[MindBridge 高风险预警]"
    tool_queue_enabled: bool = True
    tool_queue_poll_interval_seconds: float = 1.0
    tool_queue_batch_size: int = 10
    tool_queue_max_attempts: int = 3
    tool_queue_retry_delay_seconds: float = 15.0
    tool_queue_lease_seconds: float = 120.0
    mcp_tool_timeout_seconds: float = 45.0
    tool_queue_excel_workers: int = 1
    tool_queue_email_workers: int = 2
    alert_email_rate_limit_per_minute: int = 30

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
