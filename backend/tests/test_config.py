import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_refuses_a_value_from_the_environment_that_begins_with_hash(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "# openssl rand -hex 32")
    with pytest.raises(ValidationError, match="JWT_SECRET") as excinfo:
        Settings(_env_file=None)
    assert "openssl" not in str(excinfo.value)


def test_refuses_a_value_from_the_env_file_that_begins_with_hash(tmp_path, monkeypatch):
    monkeypatch.delenv("INTERNAL_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("APP_ENV=development\nINTERNAL_API_KEY=#not-a-key\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="INTERNAL_API_KEY"):
        Settings(_env_file=env_file)


def test_loads_a_clean_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment on its own line\nCORS_ORIGINS=http://localhost:3000, https://example.test\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert settings.cors_origin_list == ["http://localhost:3000", "https://example.test"]
