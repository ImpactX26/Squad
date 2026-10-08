"""Settings from backend/.env (ARCHITECTURE.md §13.2). The environment wins over the file."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    # hide_input_in_errors: a validation error would otherwise print every value, secrets included.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    # ---------- App ----------
    app_env: str = "development"
    app_name: str = "ServiceMesh"
    company_name: str = "Aurora Devices"
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_gstin: str = ""
    frontend_url: str = "http://localhost:3000"
    backend_url: str = "http://127.0.0.1:8000"
    cors_origins: str = "http://localhost:3000"
    jwt_secret: str = ""
    jwt_expire_minutes: int = 720
    internal_api_key: str = ""
    seed_staff_password: str = ""

    # ---------- Database ----------
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/servicemesh"

    # ---------- LLM (free tiers only) ----------
    llm_provider: str = "groq"
    llm_fallback_provider: str = "ollama"
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    model_fast: str = "qwen/qwen3.8-27b"
    model_smart: str = "openai/gpt-oss-20b"
    groq_reasoning_effort: str = "low"
    groq_qwen_reasoning_effort: str = "none"
    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout_seconds: float = 60
    llm_max_tokens_fast: int = 300
    llm_max_tokens_smart: int = 800
    ai_max_tool_iterations: int = 6
    ai_timeout_seconds: float = 15

    # ---------- Decisions (Jev, optional) ----------
    decision_provider: str = "llm"
    jev_api_key: str = ""
    jev_base_url: str = "https://api.typesafe.ai"
    jev_path: str = "/v1/systemone"
    jev_model: str = "jev-latest"
    jev_timeout_seconds: float = 3
    decision_min_confidence: float = 0.6

    # ---------- Embeddings / search ----------
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    duplicate_similarity_threshold: float = 0.82
    duplicate_lookback_days: int = 30

    # ---------- Channel switches ----------
    enable_discord: bool = False
    enable_telegram: bool = False
    enable_email: bool = False

    # ---------- Discord ----------
    discord_bot_token: str = ""
    discord_application_id: str = ""
    discord_guild_id: str = ""
    discord_support_channel_id: str = ""

    # ---------- Telegram ----------
    telegram_bot_token: str = ""

    # ---------- Email (Gmail) ----------
    email_address: str = ""
    email_app_password: str = ""
    email_from_name: str = "Aurora Support"
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    email_poll_seconds: float = 10
    email_redirect_to: str = ""

    # ---------- Payments (UPI QR + UTR, §7.6) ----------
    payment_link_ttl_minutes: int = 60
    upi_id: str = ""
    upi_payee_name: str = ""
    bank_alert_subject: str = "UPI-Verify"
    bank_alert_from: str = ""
    bank_secret: str = ""
    bank_poll_seconds: float = 10
    payment_verify_timeout_minutes: int = 15

    # ---------- Operations ----------
    warehouse_alert_email: str = ""
    max_jobs_per_tech_per_day: int = 4

    # ---------- MCP servers ----------
    mcp_tickets_url: str = "http://127.0.0.1:8101/mcp"
    mcp_catalog_url: str = "http://127.0.0.1:8102/mcp"
    mcp_knowledge_url: str = "http://127.0.0.1:8103/mcp"
    mcp_messaging_url: str = "http://127.0.0.1:8104/mcp"
    mcp_payments_url: str = "http://127.0.0.1:8105/mcp"
    mcp_dispatch_url: str = "http://127.0.0.1:8106/mcp"
    mcp_inventory_url: str = "http://127.0.0.1:8107/mcp"

    @model_validator(mode="before")
    @classmethod
    def refuse_comment_values(cls, data: Any) -> Any:
        # `KEY=   # comment` makes the comment the value (§18.1). Never echo the value: it may be a secret.
        if isinstance(data, dict):
            bad = sorted(
                str(key).upper()
                for key, value in data.items()
                if isinstance(value, str) and value.lstrip().startswith("#")
            )
            if bad:
                raise ValueError(
                    f"{', '.join(bad)} begin(s) with '#'. Put .env comments on their own line (§18.1)."
                )
        return data

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
