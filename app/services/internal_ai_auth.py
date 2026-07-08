"""
Internal AI auth (PERPETUA-INTERNAL-AUTH-01).

Server-side HMAC-SHA256 mechanism that lets Perpétua (the CRM AI) call the
system's own /api/ routes *on behalf of the logged-in user* without requiring
each user to manually generate an API Key.

Design:
- The AI service (app/services/ai_tools.py::call_internal_api) SIGNS each internal
  request with the backend-only secret INTERNAL_AI_AUTH_SECRET.
- The auth dependency (app/auth.py::get_current_user) VERIFIES the signature and,
  on success, loads and returns the real User by id — so downstream role checks
  (require_admin, ownership filters) apply normally and attribution is preserved.

This module is intentionally pure stdlib (no app imports) so it can be shared by
both the signer and the verifier without circular-import risk. The secret is
always passed in as an argument — never read here — so callers control config
lookup and tests can inject values.

The signed message binds: user_id, timestamp, HTTP method, and the bare request
path (query string excluded, so the signer and the server-side request.url.path
always agree). It does NOT bind the request body — the mechanism authenticates
*who* is calling and roughly *what* endpoint, not full payload integrity; the
internal channel is loopback-only (127.0.0.1) and the timestamp bounds replay.
"""
import hashlib
import hmac
import time

# Header names used on the wire for the internal AI auth path.
HEADER_USER_ID = "X-Internal-AI-User-Id"
HEADER_TIMESTAMP = "X-Internal-AI-Timestamp"
HEADER_SIGNATURE = "X-Internal-AI-Signature"


def _canonical_path(path: str) -> str:
    """Bare path used in the signature (query string stripped)."""
    return (path or "").split("?", 1)[0]


def build_signing_message(user_id, timestamp, method: str, path: str) -> bytes:
    """Canonical message that both signer and verifier hash. Must stay identical
    on both sides — change here means change everywhere."""
    return (
        f"{user_id}\n{timestamp}\n{(method or '').upper()}\n{_canonical_path(path)}"
    ).encode("utf-8")


def sign_internal_request(secret: str, user_id, timestamp, method: str, path: str) -> str:
    """Return the hex HMAC-SHA256 signature for an internal AI request."""
    msg = build_signing_message(user_id, timestamp, method, path)
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_internal_signature(
    secret: str,
    user_id,
    timestamp,
    method: str,
    path: str,
    signature: str,
    max_skew_seconds: int,
):
    """Validate an internal AI signature.

    Returns (ok: bool, reason: str). Rejects (never raises) when the secret is
    missing, any field is absent, the timestamp is malformed or outside the skew
    window, or the signature does not match (constant-time compare).
    """
    if not secret:
        return False, "secret_not_configured"
    if not (user_id and timestamp and signature):
        return False, "missing_fields"
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False, "bad_timestamp"
    now = int(time.time())
    try:
        skew = int(max_skew_seconds)
    except (TypeError, ValueError):
        skew = 300
    if abs(now - ts) > skew:
        return False, "expired"
    expected = sign_internal_request(secret, user_id, timestamp, method, path)
    if not hmac.compare_digest(expected, str(signature)):
        return False, "bad_signature"
    return True, "ok"
