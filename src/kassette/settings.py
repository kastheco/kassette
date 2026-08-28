"""Runtime configuration loaded from the local environment."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class KassetteSettings(BaseSettings):
    """Credentials and provider choices for the cascaded voice pipeline."""

    model_config = SettingsConfigDict(
        env_file=Path(".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    voice_backend: Literal["cascade", "quicksilver"] = Field(
        default="cascade",
        validation_alias="KASSETTE_VOICE_BACKEND",
    )
    vad_stop_secs: float = Field(
        default=1.8,
        gt=0,
        validation_alias="KASSETTE_VAD_STOP_SECS",
    )
    transcript_grooming_profile: Path | None = Field(
        default=None,
        validation_alias="KASSETTE_TRANSCRIPT_GROOMING_PROFILE",
    )
    transcript_grooming_timeout_secs: float = Field(
        default=0.5,
        gt=0,
        le=10,
        validation_alias="KASSETTE_TRANSCRIPT_GROOMING_TIMEOUT_SECS",
    )
    google_api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        validation_alias="GOOGLE_API_KEY",
    )
    fish_api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        validation_alias="FISH_API_KEY",
    )
    fish_model: str = Field(
        default="s2.1-pro",
        min_length=1,
        validation_alias="FISH_MODEL",
    )
    fish_voice_id: str | None = Field(
        default=None,
        validation_alias="FISH_VOICE_ID",
    )
    elevenlabs_api_key: SecretStr | None = Field(
        default=None,
        min_length=1,
        validation_alias="ELEVENLABS_API_KEY",
    )
    elevenlabs_voice_id: str | None = Field(
        default=None,
        min_length=1,
        validation_alias="ELEVENLABS_VOICE_ID",
    )

    def fish_credential(self) -> str:
        """Return the Fish Audio secret for live or on-demand synthesis."""
        if self.fish_api_key is None:
            raise RuntimeError("FISH_API_KEY is required for text-to-speech")
        return self.fish_api_key.get_secret_value()

    def cascade_credentials(self) -> tuple[str, str]:
        """Return provider secrets only when the cascaded backend needs them."""
        if self.google_api_key is None:
            raise RuntimeError("GOOGLE_API_KEY is required for cascaded voice")
        return (
            self.google_api_key.get_secret_value(),
            self.fish_credential(),
        )

    def comparison_credentials(self) -> tuple[str, str, str]:
        """Return the credentials needed by the three-way TTS comparison."""
        if self.elevenlabs_api_key is None:
            raise RuntimeError("ELEVENLABS_API_KEY is required for TTS comparison")
        if not self.elevenlabs_voice_id:
            raise RuntimeError("ELEVENLABS_VOICE_ID is required for TTS comparison")
        if self.fish_api_key is None:
            raise RuntimeError("FISH_API_KEY is required for TTS comparison")
        return (
            self.elevenlabs_api_key.get_secret_value(),
            self.elevenlabs_voice_id,
            self.fish_api_key.get_secret_value(),
        )


def load_settings(*, env_file: Path | str = ".env") -> KassetteSettings:
    """Load settings without exposing secret values in errors or logs."""
    return KassetteSettings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
