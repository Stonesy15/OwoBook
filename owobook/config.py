"""App configuration, loaded from environment variables and `.env`."""

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    timezone: str = "Africa/Lagos"
    database_url: str = "sqlite:///./owobook.db"
    allowed_wa_ids: str = ""
    # Public address for private ledger links; Render sets RENDER_EXTERNAL_URL by itself.
    public_base_url: str = Field("", validation_alias=AliasChoices("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"))
    link_secret: str = ""  # signs ledger links; falls back to WA_APP_SECRET

    # LLM provider selection
    llm_provider: Literal["meta", "baseten", "fake"] = "meta"
    extraction_provider: Literal["meta", "baseten", "fake"] = "baseten"

    # Meta Model API
    model_api_key: str = ""
    meta_api_base: str = "https://api.meta.ai/v1"
    meta_chat_model: str = "muse-spark-1.3"
    meta_transcribe_model: str = "muse-voice-transcribe-1.0"
    meta_reasoning_effort: str = "low"  # Muse Spark always reasons; "low" keeps WhatsApp replies fast
    llm_timeout_seconds: float = 45.0

    # Transcription selection
    transcriber: Literal["meta", "fake"] = "meta"

    # Baseten (Meta open-weights models)
    baseten_api_key: str = ""
    baseten_base_url: str = "https://inference.baseten.co/v1"
    baseten_model: str = ""

    # WhatsApp Cloud API
    wa_access_token: str = ""
    wa_phone_number_id: str = ""
    wa_app_secret: str = ""
    wa_verify_token: str = ""
    wa_graph_version: str = "v26.0"  # v21.0 is removed on 21 Jan 2027
    wa_reminder_template: str = ""
    wa_reminder_template_lang: str = "en"

    # Reminders
    enable_scheduler: bool = True  # daily job inside the web server
    reminder_hour: int = 8  # Africa/Lagos
    task_token: str = ""  # lets an external cron call POST /tasks/reminders (header X-Task-Token)

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        """Supabase/Render give `postgres://` URLs; SQLAlchemy + psycopg3 needs `postgresql+psycopg://`."""
        if v.startswith("postgres://"):
            return "postgresql+psycopg://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            return "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def allowed_wa_id_set(self) -> set[str]:
        """Parsed allow-list, digits only ("+234 801-234" -> "234801234", like WhatsApp's wa_id).
        Empty set means everyone is allowed."""
        ids = ("".join(c for c in x if c.isdigit()) for x in self.allowed_wa_ids.split(","))
        return {x for x in ids if x}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def mask_secret(value: str, keep: int = 4) -> str:
    """Show only the last few characters of a secret, for safe logging."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * 6 + value[-keep:]
