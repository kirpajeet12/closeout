"""Configuration and model factory. Secrets come from the environment only."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    provider: str = os.environ.get("CLOSEOUT_MODEL_PROVIDER", "bedrock")
    model_id: str = os.environ.get("CLOSEOUT_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    fast_model_id: str = os.environ.get("CLOSEOUT_FAST_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    region: str = os.environ.get("AWS_REGION", "us-east-1")
    data_dir: Path = Path(os.environ.get("CLOSEOUT_DATA_DIR", "./data")).resolve()
    max_tokens: int = int(os.environ.get("CLOSEOUT_MAX_TOKENS", "2000"))
    # how messages to the contractor are signed; never a person's name
    office: str = os.environ.get("CLOSEOUT_OFFICE", "the engineer's office")
    # one shared office code for the live site; empty = open (local use). Contractor links never need it.
    access_code: str = os.environ.get("CLOSEOUT_ACCESS_CODE", "")
    # spoken answers: an OpenAI key turns on the natural voice; without it the phone's own voice reads the answer
    voice_key: str = os.environ.get("OPENAI_API_KEY", "")
    voice_model: str = os.environ.get("CLOSEOUT_VOICE_MODEL", "gpt-4o-mini-tts")
    voice_name: str = os.environ.get("CLOSEOUT_VOICE", "marin")
    # the spoken conversation (ears and mouth); the same key turns it on. The Closeout agent stays the brain.
    live_model: str = os.environ.get("CLOSEOUT_LIVE_MODEL", "gpt-live-1")


SETTINGS = Settings()


def make_model(settings: Settings = SETTINGS, fast: bool = False):
    """Return a Strands model object for the configured provider."""
    model_id = settings.fast_model_id if fast else settings.model_id
    if settings.provider == "anthropic":
        from strands.models.anthropic import AnthropicModel

        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("CLOSEOUT_MODEL_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
        # Anthropic ids are not the Bedrock ids; allow override, default to Sonnet 4.6 direct.
        direct_id = os.environ.get("CLOSEOUT_ANTHROPIC_MODEL_ID", "claude-sonnet-4-6")
        return AnthropicModel(client_args={"api_key": key}, model_id=direct_id, max_tokens=settings.max_tokens)
    if settings.provider == "bedrock":
        from strands.models import BedrockModel

        return BedrockModel(model_id=model_id, region_name=settings.region, max_tokens=settings.max_tokens)
    raise RuntimeError(f"Unknown CLOSEOUT_MODEL_PROVIDER: {settings.provider}")
