"""AWS credential resolution for Transcribe streaming."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import botocore.session
from botocore.exceptions import BotoCoreError

DEFAULT_AWS_REGION = "us-east-1"


class AWSCredentialsUnavailableError(RuntimeError):
    code = "aws_credentials_unavailable"


@dataclass(frozen=True, slots=True)
class AWSTranscribeCredentials:
    """Frozen credentials for one Transcribe stream. Secrets stay out of repr."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(repr=False)
    region: str


def resolve_aws_transcribe_credentials(*, region: str | None) -> AWSTranscribeCredentials:
    """Resolve credentials and region from the standard AWS chain.

    Credentials come from botocore's provider chain, which covers `AWS_PROFILE`,
    `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, shared config and credential files,
    and SSO. Region precedence is the explicit Kassette setting, `AWS_REGION`, the
    botocore region (`AWS_DEFAULT_REGION` or the profile), then `us-east-1`.
    """
    try:
        session = botocore.session.Session()
        resolved_region = (
            region
            or os.environ.get("AWS_REGION")
            or session.get_config_variable("region")
            or DEFAULT_AWS_REGION
        )
        credentials = session.get_credentials()
        frozen = credentials.get_frozen_credentials() if credentials is not None else None
    except (BotoCoreError, ValueError) as error:
        raise AWSCredentialsUnavailableError(
            f"AWS credentials could not be loaded: {error}. "
            "check AWS_PROFILE, or run `aws sso login` if the profile uses SSO"
        ) from None
    if frozen is None or not frozen.access_key or not frozen.secret_key:
        raise AWSCredentialsUnavailableError(
            "AWS credentials are required for AWS transcription. set AWS_PROFILE to a "
            "configured profile or export AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
        )
    return AWSTranscribeCredentials(
        access_key_id=frozen.access_key,
        secret_access_key=frozen.secret_key,
        session_token=frozen.token or None,
        region=str(resolved_region),
    )
