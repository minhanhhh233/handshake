"""Error format and codes (document 03, section 1.2). Shared contract (document 08, section 2).

Errors are `application/problem+json`. Raise `HandshakeError` (or a helper below) anywhere;
the API layer turns it into a response.
"""

from __future__ import annotations

from typing import Any

PROBLEM_TYPE_BASE = "https://handshake.example/errors/"

# code -> (HTTP status, title)
ERROR_CODES: dict[str, tuple[int, str]] = {
    "invalid_request": (400, "Invalid request"),
    "unauthenticated": (401, "Unauthenticated"),
    "forbidden": (403, "Forbidden"),
    "agent_suspended": (403, "Agent suspended"),
    "not_found": (404, "Not found"),
    "offer_expired": (409, "Offer expired"),
    "offer_consumed": (409, "Offer consumed"),
    "idempotency_conflict": (409, "Idempotency conflict"),
    "no_candidates": (422, "No candidates"),
    "rate_limited": (429, "Rate limited"),
    "upstream_unavailable": (503, "Upstream unavailable"),
}


class HandshakeError(Exception):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        next: str | None = None,
        extra: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(detail)
        self.code = code
        self.status = status or ERROR_CODES[code][0]
        self.title = ERROR_CODES[code][1]
        self.detail = detail
        self.next = next
        self.extra = extra or {}

    def problem(self, trace_id: str | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": PROBLEM_TYPE_BASE + self.code,
            "title": self.title,
            "status": self.status,
            "code": self.code,
            "detail": self.detail,
            "trace_id": trace_id,
        }
        if self.next:
            body["next"] = self.next
        body.update(self.extra)
        return body


def invalid_request(detail: str, errors: list[dict[str, Any]] | None = None) -> HandshakeError:
    return HandshakeError("invalid_request", detail, extra={"errors": errors or []})


def unauthenticated(detail: str = "Missing or unknown agent key.",
                    next: str = "Send Authorization: Bearer <agent_key>.") -> HandshakeError:
    return HandshakeError("unauthenticated", detail, next=next)


def forbidden(detail: str = "This resource belongs to another agent.") -> HandshakeError:
    return HandshakeError("forbidden", detail)


def not_found(detail: str) -> HandshakeError:
    return HandshakeError("not_found", detail)


def offer_expired(offer_id: str, expires_at_iso: str) -> HandshakeError:
    return HandshakeError(
        "offer_expired",
        f"Offer {offer_id} expired at {expires_at_iso}.",
        next="Request a new offer with POST /v1/offers.",
    )


def offer_consumed(offer_id: str) -> HandshakeError:
    return HandshakeError(
        "offer_consumed",
        f"Offer {offer_id} has already been used for an order.",
        next="Request a new offer with POST /v1/offers.",
    )


def idempotency_conflict() -> HandshakeError:
    return HandshakeError(
        "idempotency_conflict",
        "This Idempotency-Key was already used with a different request body.",
        next="Use a new Idempotency-Key for a different order.",
    )


def no_candidates(excluded: list[dict[str, Any]]) -> HandshakeError:
    return HandshakeError(
        "no_candidates",
        "No requested product can be priced or delivered. See excluded[].",
        next="Change the products, quantity or delivery date and request a new offer.",
        extra={"excluded": excluded},
    )


def rate_limited(detail: str = "Too many requests.") -> HandshakeError:
    return HandshakeError("rate_limited", detail, next="Wait a minute and retry.")


def upstream_unavailable(detail: str = "Valio Aimo API failed or timed out.") -> HandshakeError:
    return HandshakeError(
        "upstream_unavailable",
        detail,
        next="Retry shortly. Offers are never issued from stale data.",
    )
