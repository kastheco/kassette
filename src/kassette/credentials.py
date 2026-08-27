"""Credential providers for experimental GPT-Live access."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast


class CredentialUnavailableError(RuntimeError):
    code = "codex_credentials_unavailable"


@dataclass(frozen=True, slots=True)
class CodexCredentials:
    access_token: str = field(repr=False)
    account_id: str


class CodexCredentialProvider(Protocol):
    async def load(self) -> CodexCredentials: ...


def default_pi_auth_path() -> Path:
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
    return agent_dir / "auth.json"


class PiAuthCredentialProvider:
    """Read existing openai-codex OAuth credentials without copying them."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_pi_auth_path()

    async def load(self) -> CodexCredentials:
        return await asyncio.to_thread(self._read)

    def _read(self) -> CodexCredentials:
        try:
            raw = cast(object, json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise CredentialUnavailableError(
                "openai-codex credentials are unavailable; authenticate in Pi first"
            ) from error

        root = _record(raw)
        entry = _record(root.get("openai-codex")) if root else None
        if entry is None or entry.get("type") != "oauth":
            raise CredentialUnavailableError(
                "openai-codex OAuth credentials are unavailable; authenticate in Pi first"
            )

        expires = entry.get("expires")
        if isinstance(expires, int | float) and time.time() * 1000 >= expires:
            raise CredentialUnavailableError(
                "openai-codex OAuth credentials have expired; authenticate in Pi again"
            )

        access = entry.get("access")
        account = entry.get("accountId") or entry.get("account_id")
        access_token = access.strip() if isinstance(access, str) else ""
        account_id = account.strip() if isinstance(account, str) else ""
        if access_token and not account_id:
            account_id = _account_id_from_jwt(access_token) or ""
        if not access_token or not account_id:
            raise CredentialUnavailableError(
                "openai-codex credentials are incomplete; authenticate in Pi again"
            )
        return CodexCredentials(access_token=access_token, account_id=account_id)


def _account_id_from_jwt(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        decoded = _record(
            cast(object, json.loads(base64.urlsafe_b64decode(padded).decode("utf-8")))
        )
        auth = _record(decoded.get("https://api.openai.com/auth")) if decoded else None
        account = auth.get("chatgpt_account_id") if auth else None
        return account.strip() if isinstance(account, str) and account.strip() else None
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)
