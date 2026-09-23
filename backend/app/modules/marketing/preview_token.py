"""Short-lived, payload-bound promotion preview proof."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from uuid import uuid4

from app.core.errors import PromotionPreviewRequiredError
from app.modules.marketing.schemas import PromotionDraft

PREVIEW_TTL = timedelta(minutes=15)


def _canonical(draft: PromotionDraft) -> bytes:
    return json.dumps(
        draft.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def issue_preview_token(draft: PromotionDraft, *, merchant_id: int, now: datetime, secret: str) -> str:
    claims = {
        "merchant_id": merchant_id,
        "payload_hash": hashlib.sha256(_canonical(draft)).hexdigest(),
        "expires_at": int((now + PREVIEW_TTL).timestamp()),
        "nonce": uuid4().hex,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"{body.decode()}.{signature}"


def verify_preview_token(
    token: str, draft: PromotionDraft, *, merchant_id: int, now: datetime, secret: str
) -> str:
    """Return the token digest for a one-create DB uniqueness guard."""
    try:
        body, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        padded = body + "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        if (
            claims["merchant_id"] != merchant_id
            or claims["payload_hash"] != hashlib.sha256(_canonical(draft)).hexdigest()
            or claims["expires_at"] <= int(now.timestamp())
        ):
            raise ValueError("claims")
    except (ValueError, KeyError, TypeError):
        raise PromotionPreviewRequiredError("preview this exact promotion before creating it") from None
    return hashlib.sha256(token.encode()).hexdigest()
