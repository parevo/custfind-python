import hashlib
import hmac
import json
import time
from typing import Any, Dict, Optional, Union


class WebhookVerificationError(Exception):
    """A webhook delivery that did not verify."""


def verify_webhook(
    raw_body: Union[bytes, str],
    signature_header: Optional[str],
    secret: str,
    tolerance_seconds: int = 300,
) -> Dict[str, Any]:
    """Verify a delivery and return its event.

    Pass the raw request body exactly as received (before JSON parsing), the
    Custfind-Signature header and your endpoint's whsec_ secret.
    """
    body = raw_body.encode() if isinstance(raw_body, str) else raw_body
    timestamp = 0
    signatures = []
    for part in (signature_header or "").split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = 0
        elif key == "v1" and value:
            signatures.append(value)
    if not timestamp or not signatures:
        raise WebhookVerificationError("malformed Custfind-Signature header")
    if abs(time.time() - timestamp) > tolerance_seconds:
        raise WebhookVerificationError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise WebhookVerificationError("signature mismatch")
    return json.loads(body)
