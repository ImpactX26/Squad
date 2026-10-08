"""Settings loaded from backend/.env (see ARCHITECTURE.md §13.2).

Every variable in backend/.env.example is required here with no default, so the
.env file is the single source of truth and a missing key fails at startup.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent

# A UPI virtual payment address: handle@psp, e.g. aurora-devices@okaxis.
UPI_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,255}@[A-Za-z][A-Za-z0-9]{1,63}")
EMAIL_ADDRESS = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")
MIN_BANK_SECRET_CHARS = 8


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- App ----------
    app_env: str
    app_name: str
    company_name: str
    # Printed on the PDF receipt (§7.6) when set; an empty one is left off. The only optional keys
    # here, so a .env written before the receipt existed still starts.
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    company_gstin: str = ""
    frontend_url: str
    backend_url: str
    cors_origins: str
    jwt_secret: str = Field(min_length=32)
    jwt_expire_minutes: int
    internal_api_key: str
    seed_staff_password: str

    # ---------- Database ----------
    database_url: str

    # ---------- LLM (free tiers only) ----------
    llm_provider: Literal["groq", "ollama"]
    llm_fallback_provider: Literal["groq", "ollama", ""]
    groq_api_key: str
    groq_base_url: str
    model_fast: str
    model_smart: str
    groq_reasoning_effort: Literal["low", "medium", "high", ""]
    groq_qwen_reasoning_effort: Literal["none", "default", "low", "medium", "high", ""]
    ollama_base_url: str
    ollama_model: str
    ollama_timeout_seconds: int
    llm_max_tokens_fast: int
    llm_max_tokens_smart: int
    ai_max_tool_iterations: int
    ai_timeout_seconds: int

    # ---------- Decisions (Jev, optional) ----------
    decision_provider: Literal["jev", "llm"]
    jev_api_key: str
    jev_base_url: str
    jev_path: str
    jev_model: str
    jev_timeout_seconds: float
    decision_min_confidence: float

    # ---------- Embeddings / search ----------
    embedding_model: str
    embedding_dim: int
    duplicate_similarity_threshold: float
    duplicate_lookback_days: int

    # ---------- Channel switches ----------
    enable_discord: bool
    enable_telegram: bool
    enable_email: bool

    # ---------- Discord ----------
    discord_bot_token: str
    discord_application_id: str
    discord_guild_id: str
    discord_support_channel_id: str

    # ---------- Telegram ----------
    telegram_bot_token: str

    # ---------- Email (Gmail) ----------
    email_address: str
    email_app_password: str
    email_from_name: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    email_poll_seconds: int
    # Development only: every outgoing email goes here instead (the real recipient is in the subject).
    email_redirect_to: str

    # ---------- Payments (UPI QR + UTR, §7.6) ----------
    # The provider (upi_utr) and the currency (INR) are constants in app/payments/money.py.
    payment_link_ttl_minutes: int = Field(gt=0)
    upi_id: str
    upi_payee_name: str
    bank_alert_subject: str
    bank_alert_from: str
    bank_secret: str
    bank_poll_seconds: int = Field(gt=0)
    payment_verify_timeout_minutes: int = Field(gt=0)

    # ---------- Operations ----------
    warehouse_alert_email: str
    max_jobs_per_tech_per_day: int

    # ---------- MCP servers ----------
    mcp_tickets_url: str
    mcp_catalog_url: str
    mcp_knowledge_url: str
    mcp_messaging_url: str
    mcp_payments_url: str
    mcp_dispatch_url: str
    mcp_inventory_url: str

    @field_validator("*", mode="before")
    @classmethod
    def _reject_parsed_comment(cls, v: object) -> object:
        # python-dotenv reads `KEY=   # comment` as the value "# comment", because the
        # whitespace after `=` is consumed before the inline-comment check. Without this,
        # an unfilled secret in a copied .env.example silently becomes public comment text.
        if isinstance(v, str) and v.lstrip().startswith("#"):
            raise ValueError("value is the inline comment from .env.example; set a real value or leave it empty")
        return v

    # Each payment value may be empty (the rest of the app runs without UPI), but a value that is
    # set must be usable: a typo here would otherwise surface as a payment nobody can verify.

    @field_validator("upi_id")
    @classmethod
    def _upi_id_is_a_vpa(cls, v: str) -> str:
        v = v.strip()
        if v and not UPI_ID.fullmatch(v):
            raise ValueError("UPI_ID must look like name@bank (a UPI virtual payment address)")
        return v

    @field_validator("bank_secret")
    @classmethod
    def _bank_secret_is_long_enough(cls, v: str) -> str:
        v = v.strip()
        if v and len(v) < MIN_BANK_SECRET_CHARS:
            raise ValueError(f"BANK_SECRET must be at least {MIN_BANK_SECRET_CHARS} characters, or empty")
        return v

    @field_validator("bank_alert_from")
    @classmethod
    def _bank_alert_from_is_addresses(cls, v: str) -> str:
        for address in (a.strip() for a in v.split(",")):
            if address and not EMAIL_ADDRESS.fullmatch(address):
                raise ValueError(f"BANK_ALERT_FROM entry {address!r} is not an email address")
        return v

    @field_validator("email_redirect_to")
    @classmethod
    def _redirect_is_an_address(cls, v: str) -> str:
        v = v.strip()
        if v and not EMAIL_ADDRESS.fullmatch(v):
            raise ValueError("EMAIL_REDIRECT_TO must be one email address, or empty")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def bank_alert_senders(self) -> frozenset[str]:
        """BANK_ALERT_FROM as lower-case addresses: the only senders a bank alert is accepted from."""
        return frozenset(a.strip().lower() for a in self.bank_alert_from.split(",") if a.strip())

    @property
    def upi_configured(self) -> bool:
        """Whether a payment link can be paid: the QR needs a UPI ID and a payee name."""
        return bool(self.upi_id.strip() and self.upi_payee_name.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()