"""The few backend/.env keys the MCP servers read (ARCHITECTURE.md §13.2). The environment wins over the file."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class MCPSettings(BaseSettings):
    # hide_input_in_errors: a validation error would otherwise print the values, secrets included.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/servicemesh"
    backend_url: str = "http://127.0.0.1:8000"
    internal_api_key: str = ""


@lru_cache
def get_settings() -> MCPSettings:
    return MCPSettings()
