"""Project-policy enforcement.

The runner consults this module before each step:

* network requests are gated by origin/method/path allowlist;
* model disclosure gates whether page text, request bodies, and screenshots
  may be sent to the hosted model and saved in evidence;
* policy violations become BLOCKED, not silent skips.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from pydantic import HttpUrl

from .models import ModelDisclosurePolicy, Policy


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str | None = None


class PolicyEnforcer:
    """Apply a :class:`Policy` to network and disclosure decisions."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    # ----- network -------------------------------------------------------
    def check_request(self, method: str, url: str) -> PolicyDecision:
        try:
            parts = urlsplit(url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
            ):
                return PolicyDecision(False, "Only authorized HTTP(S) origins without credentials are permitted")
            if self.policy.goal_only:
                validated = HttpUrl(url)
                if validated.username is not None or validated.password is not None:
                    return PolicyDecision(False, "Only HTTP(S) URLs without credentials are permitted")
                return PolicyDecision(True)
            host = parts.hostname.encode("idna").decode("ascii").lower()
            if ":" in host:
                host = f"[{host}]"
            port = parts.port
            suffix = f":{port}" if port and port != (443 if parts.scheme == "https" else 80) else ""
            origin = f"{parts.scheme}://{host}{suffix}"
            path = parts.path or "/"
        except (ValueError, UnicodeError):
            return PolicyDecision(False, "Invalid request URL")
        if "\\" in path or any(segment in {".", ".."} for segment in path.split("/")):
            return PolicyDecision(False, "Ambiguous request path")
        if re.search(r"%(?:25|2e|2f|5c)|%(?![0-9a-f]{2})", path, re.IGNORECASE):
            return PolicyDecision(False, "Encoded path separators are not authorized")
        if not self.policy.network.permits(method.upper(), origin, path):
            return PolicyDecision(False, "Request origin, method, or path is outside project policy")
        return PolicyDecision(True)

    # ----- model disclosure ---------------------------------------------
    def may_disclose(self, what: str) -> bool:
        disclosure = self.policy.model_disclosure
        return {
            "page_text": disclosure.allow_page_text,
            "action_history": disclosure.allow_action_history,
            "request_body": disclosure.allow_request_bodies,
            "response_body": disclosure.allow_response_bodies,
            "screenshot": disclosure.allow_screenshots,
        }.get(what, False)

    def filter_disclosable_state(self, state: Mapping[str, Any]) -> dict[str, Any]:
        """Strip non-disclosable fields before sending state to the model."""

        if not self.may_disclose("page_text"):
            return {"page": {}, "elements": [], "recent_actions": []}
        page = state.get("page") or {}
        return {
            "page": {key: page.get(key, "") for key in ("url", "title", "text")},
            "elements": state.get("elements", []),
            "recent_actions": state.get("recent_actions", []) if self.may_disclose("action_history") else [],
        }


def deny_reason(disclosure: ModelDisclosurePolicy, what: str) -> str:
    return f"policy does not permit disclosure of {what!r}"


__all__ = ["PolicyDecision", "PolicyEnforcer", "deny_reason"]
