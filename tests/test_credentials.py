import json
from pathlib import Path

import pytest

from kassette.credentials import CredentialUnavailableError, PiAuthCredentialProvider


async def test_pi_auth_provider_reads_oauth_without_exposing_token(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "secret-token",
                    "accountId": "account-1",
                    "expires": 9_999_999_999_999,
                }
            }
        )
    )

    credentials = await PiAuthCredentialProvider(path).load()

    assert credentials.access_token == "secret-token"
    assert credentials.account_id == "account-1"
    assert "secret-token" not in repr(credentials)


async def test_pi_auth_provider_rejects_expired_credentials(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text(
        json.dumps(
            {
                "openai-codex": {
                    "type": "oauth",
                    "access": "secret-token",
                    "accountId": "account-1",
                    "expires": 1,
                }
            }
        )
    )

    with pytest.raises(CredentialUnavailableError, match="expired") as error:
        await PiAuthCredentialProvider(path).load()

    assert "secret-token" not in str(error.value)
