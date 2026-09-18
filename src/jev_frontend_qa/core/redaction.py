"""Sanitize persisted evidence without changing raw values used by assertions."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .models import RedactionPolicy

REDACTED = "[REDACTED]"
_SECRET_FIELDS = {"password", "passwd", "token", "secret", "apikey", "api_key", "access_token", "refresh_token"}
_SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_API_KEY = re.compile(r"\b(?:sk-|sk_live_|ts_)[A-Za-z0-9_-]{8,}\b")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key|access_token|refresh_token)\s*[=:]\s*[^\s&,;]+"
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def _redact_url(match: re.Match) -> str:
    try:
        parts = urlsplit(match.group())
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        authority = host + (f":{parts.port}" if parts.port else "")
        # Query and fragment values can carry arbitrary tokens; retain neither.
        return urlunsplit((parts.scheme, authority, parts.path, REDACTED if parts.query else "", ""))
    except ValueError:
        return REDACTED


def redact_string(text: str) -> str:
    text = _URL.sub(_redact_url, text)
    text = _BEARER.sub("Bearer " + REDACTED, text)
    text = _API_KEY.sub(REDACTED, text)
    return _SECRET_ASSIGNMENT.sub(lambda m: m[1] + "=" + REDACTED, text)


def redact_headers(headers: Mapping[str, str] | None, policy: RedactionPolicy) -> dict[str, str]:
    blocked = _SECRET_HEADERS | {name.casefold() for name in policy.redact_headers}
    return {
        name: REDACTED if name.casefold() in blocked else redact_string(str(value))
        for name, value in (headers or {}).items()
    }


def redact_body(body: Any, policy: RedactionPolicy, *, jsonpath_fields: Iterable[str] = ()) -> Any:
    fields = _SECRET_FIELDS | {name.casefold() for name in (*policy.redact_body_fields, *jsonpath_fields)}
    return _sanitize(body, fields, ())


def fixture_secrets(fixtures: Mapping[str, str], policy: RedactionPolicy) -> tuple[str, ...]:
    fields = _SECRET_FIELDS | {name.casefold() for name in policy.redact_body_fields}
    return tuple(value for name, value in fixtures.items() if value and name.casefold() in fields)


def redact_report(value: Any, policy: RedactionPolicy, *, secrets: Iterable[str] = ()) -> Any:
    fields = (
        _SECRET_FIELDS
        | _SECRET_HEADERS
        | {name.casefold() for name in (*policy.redact_body_fields, *policy.redact_headers)}
    )
    known = tuple(sorted({secret for secret in secrets if secret}, key=len, reverse=True))
    return _sanitize(value, fields, known)


def _sanitize(value: Any, fields: set[str], secrets: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: REDACTED if str(key).casefold() in fields else _sanitize(item, fields, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_sanitize(item, fields, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        return redact_string(value)
    return value
