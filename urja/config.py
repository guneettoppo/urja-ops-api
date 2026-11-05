"""Configuration, loaded from environment / .env with sane defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # --- portal (the legacy system we integrate with) ---
    portal_base_url: str = "https://urja-ops.flockenergy.tech"
    portal_email: str = ""
    portal_password: str = ""

    # --- our service ---
    database_path: Path = REPO_ROOT / "data" / "urja.db"
    seed_db_path: Path = REPO_ROOT / "seed" / "urja.db"
    """Pre-built index copied into place when ``database_path`` is empty.

    Free hosting tiers have an ephemeral filesystem, so the index is lost on
    every restart. Seeding makes a cold start instant instead of a four-minute
    sync; with live portal data this would be wrong, but this dataset is frozen.
    """

    seed_on_empty: bool = True
    request_timeout_seconds: float = 20.0
    """Per-request HTTP timeout against the portal."""

    max_concurrency: int = 2
    """Simultaneous in-flight requests to the portal. Kept low on purpose."""

    polite_delay_ms: int = 120
    """Minimum spacing between portal requests (the portal 429s on bursts)."""

    sync_interval_minutes: int = 60
    auto_sync_on_start: bool = True
    sync_energy_on_start: bool = True

    log_level: str = "INFO"


settings = Settings()
