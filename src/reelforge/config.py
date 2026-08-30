"""Load configuration from config.yaml + .env into one object.

Mirrors the pattern used elsewhere: a single ``load_config()`` entry point returning
a plain object, so modules do ``from reelforge.config import CONFIG``-style access via
``get_config()`` rather than re-parsing files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Project root = two levels up from this file (src/reelforge/config.py -> project root).
ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Secrets:
    gemini_api_key: str
    youtube_client_secrets_file: Path
    youtube_token_file: Path
    ig_user_id: str
    ig_access_token: str
    ig_graph_version: str
    # Cloudflare R2 (S3-compatible) — temporary public host for IG Reel uploads
    r2_endpoint: str
    r2_bucket: str
    r2_access_key: str
    r2_secret_key: str
    r2_public_base: str
    telegram_bot_token: str
    telegram_chat_id: str


@dataclass
class Config:
    raw: dict[str, Any]
    secrets: Secrets
    root: Path

    # convenience accessors -------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def jobs_dir(self) -> Path:
        return self._abs(self.raw["paths"]["jobs_dir"])

    @property
    def logs_dir(self) -> Path:
        return self._abs(self.raw["paths"]["logs_dir"])

    @property
    def db_file(self) -> Path:
        return self._abs(self.raw["paths"]["db_file"])

    def _abs(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.root / path


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name, default)
    p = Path(raw)
    return p if p.is_absolute() else ROOT / p


@lru_cache(maxsize=1)
def get_config(config_path: str | None = None) -> Config:
    load_dotenv(ROOT / ".env")

    cfg_file = Path(config_path) if config_path else ROOT / "config.yaml"
    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))

    secrets = Secrets(
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
        youtube_client_secrets_file=_env_path(
            "YOUTUBE_CLIENT_SECRETS_FILE", "secrets/youtube_client_secret.json"
        ),
        youtube_token_file=_env_path(
            "YOUTUBE_TOKEN_FILE", "secrets/youtube_token.json"
        ),
        ig_user_id=os.environ.get("IG_USER_ID", ""),
        ig_access_token=os.environ.get("IG_ACCESS_TOKEN", ""),
        ig_graph_version=os.environ.get("IG_GRAPH_VERSION", "v21.0"),
        r2_endpoint=os.environ.get("R2_ENDPOINT", ""),
        r2_bucket=os.environ.get("R2_BUCKET", ""),
        r2_access_key=os.environ.get("R2_ACCESS_KEY", ""),
        r2_secret_key=os.environ.get("R2_SECRET_KEY", ""),
        r2_public_base=os.environ.get("R2_PUBLIC_BASE", ""),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )

    config = Config(raw=raw, secrets=secrets, root=ROOT)
    config.jobs_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    return config
