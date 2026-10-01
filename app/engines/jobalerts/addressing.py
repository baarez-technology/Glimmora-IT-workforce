"""Each individual's private inbound address.

The token is derived from the user id with an HMAC rather than stored, which
means no extra table, no migration, and an address that is stable for the life
of the account. It also cannot be enumerated: knowing somebody's user id tells
you nothing without the secret.

Rotating the secret invalidates every address at once. That is the intended
blunt instrument -- if the secret leaks, every forwarding address should stop
accepting mail.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid

from app.core.config import settings

_LOCAL_PART = "jobs"
_TOKEN_LENGTH = 20

#: jobs+<token>@<domain>
_ADDRESS = re.compile(rf"{_LOCAL_PART}\+([a-f0-9]{{{_TOKEN_LENGTH}}})@", re.I)


def _secret() -> bytes:
    # Falls back to the JWT secret so a deployment that has not set the alert
    # secret still produces stable, unguessable addresses rather than failing.
    raw = settings.JOB_ALERTS_WEBHOOK_SECRET or settings.JWT_SECRET
    return raw.encode("utf-8")


def token_for(user_id: uuid.UUID) -> str:
    """The unguessable part of one person's address."""
    digest = hmac.new(_secret(), str(user_id).encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:_TOKEN_LENGTH]


def address_for(user_id: uuid.UUID) -> str:
    """The address this person forwards their LinkedIn alerts to."""
    return f"{_LOCAL_PART}+{token_for(user_id)}@{settings.BREVO_INBOUND_DOMAIN}"


def token_from_recipients(recipients: list[str] | None) -> str | None:
    """Pull our token out of whatever Brevo reports as the recipient.

    A forwarded message can carry several recipients, and the one we care
    about may not be first.
    """
    for value in recipients or []:
        match = _ADDRESS.search(value or "")
        if match:
            return match.group(1).lower()
    return None


def matches(user_id: uuid.UUID, token: str) -> bool:
    """Whether a token belongs to this user, compared in constant time."""
    return hmac.compare_digest(token_for(user_id), token.lower())
