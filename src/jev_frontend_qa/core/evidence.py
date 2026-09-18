"""Bounded CDP evidence; Fetch.networkId links interception to Network events."""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from .models import EvidenceRecord as ReportEvidence
from .models import Policy, RedactionPolicy
from .policy import PolicyEnforcer
from .redaction import redact_body, redact_headers, redact_string


@dataclass
class EvidenceRecord:
    method: str
    url: str
    status: int | None = None
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None
    error: str | None = None
    incomplete: bool = False
    request_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_drain(cls, entry: dict) -> EvidenceRecord:
        return cls(**entry)

    def sanitized(self, policy: RedactionPolicy) -> ReportEvidence:
        return ReportEvidence(
            method=self.method,
            url=redact_string(self.url),
            status=self.status,
            request_headers=redact_headers(self.request_headers, policy),
            request_body=redact_body(self.request_body, policy),
            response_headers=redact_headers(self.response_headers, policy),
            response_body=redact_body(self.response_body, policy),
            error=redact_string(self.error) if self.error else None,
            incomplete=self.incomplete,
        )


class EvidenceCollector:
    """Retain pending exchanges until complete; make every loss observable.

    The daemon filters events by its owned session before calling this class.
    Draining returns completed exchanges only. A bounded settle timeout must
    call mark_pending_incomplete() before draining unresolved exchanges.
    """

    def __init__(
        self, buffer_size: int = 512, max_body_bytes: int = 1_048_576, max_total_bytes: int = 8_388_608
    ) -> None:
        if min(buffer_size, max_body_bytes, max_total_bytes) < 1:
            raise ValueError("Evidence limits must be positive")
        self._limit = buffer_size
        self._max_body = max_body_bytes
        self._max_total = max_total_bytes
        self._records: OrderedDict[str, EvidenceRecord] = OrderedDict()
        self._pending: set[str] = set()
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self._lost = 0
        self._denied = 0
        self._incomplete = False
        self._sequence = 0
        self._needs_post_data: set[str] = set()
        self._post_data_seen: set[str] = set()
        self._enforcer = PolicyEnforcer(Policy())

    def set_policy(self, policy: Policy) -> None:
        self._enforcer = PolicyEnforcer(policy)

    def set_rules(self, rules: list[dict]) -> None:
        self.set_policy(Policy.model_validate({"network": {"allowed_origins": rules}}))

    def policy_allows(self, method: str, url: str) -> bool:
        return self._enforcer.check_request(method, url).allowed

    def handle_event(self, method: str, params: dict, session_id: str | None) -> None:
        if method == "Network.requestWillBeSent":
            self._request(params.get("requestId"), params.get("request", {}), params.get("redirectResponse"))
        elif method == "Fetch.requestPaused":
            # Fetch IDs are NOT Network IDs. Without networkId we cannot prove
            # the request/response correlation and therefore cannot pass.
            network_id = params.get("networkId")
            if not network_id:
                self.mark_incomplete(None)
            else:
                self._request(network_id, params.get("request", {}))
        elif method == "Network.responseReceived":
            record = self._records.get(params.get("requestId"))
            if record is not None:
                response = params.get("response", {})
                record.status = int(response["status"])
                if self.policy_allows(record.method, record.url):
                    record.response_headers = self._headers(response.get("headers", {}))
        elif method == "Network.loadingFailed":
            request_id = params.get("requestId")
            record = self._records.get(request_id)
            if record is not None:
                if self._is_aborted_next_prefetch(record, params):
                    # Next.js deliberately cancels speculative route prefetches
                    # when navigation state changes. They are neither a failed
                    # application exchange nor evidence for the authored step.
                    # Do not let one make an otherwise unrelated contract
                    # uncertifiable; explicit requests remain retained below.
                    self._remove(request_id)
                    return
                record.error = str(params.get("errorText") or "network_loading_failed")
                self.mark_incomplete(request_id)

    @staticmethod
    def _is_aborted_next_prefetch(record: EvidenceRecord, params: dict) -> bool:
        return (
            record.method == "GET"
            and str(params.get("errorText") or "") == "net::ERR_ABORTED"
            and record.request_headers.get("next-router-prefetch") == "1"
        )

    def _request(self, request_id: str | None, request: dict, redirect: dict | None = None) -> None:
        if not request_id:
            self.mark_incomplete(None)
            return
        if redirect and request_id in self._records:
            # Chrome reuses the ID across redirects. Keep the preceding hop
            # separately; its unavailable response body makes evidence incomplete.
            previous = self._records.pop(request_id)
            previous.status = int(redirect.get("status", 0)) or None
            previous.incomplete = True
            previous.error = "redirect_body_unavailable"
            self._sequence += 1
            previous_id = f"{request_id}:redirect:{self._sequence}"
            self._records[previous_id] = previous
            self._sizes[previous_id] = self._sizes.pop(request_id, 0)
            self._pending.discard(request_id)
            self._needs_post_data.discard(request_id)
            self._post_data_seen.discard(request_id)
            self._incomplete = True
        record = self._records.get(request_id)
        if record is None:
            while len(self._records) >= self._limit:
                oldest = next(iter(self._records))
                self._remove(oldest)
                self._lost += 1
                self._incomplete = True
            record = EvidenceRecord(
                method=str(request.get("method", "GET")).upper(), url=str(request.get("url", "")), request_id=request_id
            )
            self._records[request_id] = record
            self._pending.add(request_id)
        if not self.policy_allows(record.method, record.url):
            # Retain only routing metadata for denied requests, not their data.
            return
        record.request_headers = self._headers(request.get("headers", {}))
        if "postData" in request and request_id not in self._post_data_seen:
            record.request_body = self._body(request_id, str(request["postData"]))
            self._post_data_seen.add(request_id)
            self._needs_post_data.discard(request_id)
        elif request.get("hasPostData") and request_id not in self._post_data_seen:
            self._needs_post_data.add(request_id)

    def attach_response_body(self, request_id: str, body: str) -> None:
        record = self._records.get(request_id)
        if record is None:
            return  # Already evicted requests have been counted as lost.
        if self.policy_allows(record.method, record.url):
            record.response_body = self._body(request_id, body)
        if request_id in self._needs_post_data:
            record.incomplete = True
            record.error = "request_body_unavailable"
        if record.status is None:
            record.incomplete = True
            record.error = "response_status_unavailable"
        self._pending.discard(request_id)
        self._incomplete |= record.incomplete

    def _body(self, request_id: str, body: str) -> Any:
        size = len(body.encode("utf-8"))
        record = self._records[request_id]
        if size > self._max_body or self._bytes + size > self._max_total:
            record.incomplete = True
            record.error = "evidence_body_limit"
            self._incomplete = True
            return None
        self._bytes += size
        self._sizes[request_id] = self._sizes.get(request_id, 0) + size
        if not body:
            return None  # Empty204/HEAD and zero-length HTTP bodies are valid.
        try:
            return json.loads(body)
        except (ValueError, TypeError):
            return body

    def mark_incomplete(self, request_id: str | None) -> None:
        self._incomplete = True
        record = self._records.get(request_id)
        if record is None:
            self._lost += 1
            return
        record.incomplete = True
        record.error = record.error or "response_body_unavailable"
        self._pending.discard(request_id)

    def mark_pending_incomplete(self) -> None:
        for request_id in tuple(self._pending):
            record = self._records[request_id]
            record.error = record.error or "request_pending_at_capture_deadline"
            self.mark_incomplete(request_id)

    def mark_denied(self, method: str, url: str) -> None:
        self._denied += 1
        for request_id, record in reversed(self._records.items()):
            if record.method == method and record.url == url:
                record.error = "denied_by_policy"
                record.request_headers = {}
                record.request_body = None
                self.mark_incomplete(request_id)
                return
        self._sequence += 1
        request_id = f"denied:{self._sequence}"
        self._request(request_id, {"method": method, "url": url})
        self._records[request_id].error = "denied_by_policy"
        self.mark_incomplete(request_id)

    def drain(self) -> tuple[list[dict], int]:
        completed = [key for key in self._records if key not in self._pending]
        out = [self._records[key].to_dict() for key in completed]
        for key in completed:
            self._remove(key)
        lost, self._lost = self._lost, 0
        return out, lost

    def _remove(self, request_id: str) -> None:
        self._records.pop(request_id, None)
        self._pending.discard(request_id)
        self._needs_post_data.discard(request_id)
        self._post_data_seen.discard(request_id)
        self._bytes -= self._sizes.pop(request_id, 0)

    @staticmethod
    def _headers(headers: dict) -> dict[str, str]:
        result = {str(key): str(value) for key, value in headers.items()}
        if sum(len(key) + len(value) for key, value in result.items()) > 65_536:
            raise ValueError("Evidence header limit exceeded")
        return result

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def incomplete(self) -> bool:
        return self._incomplete

    @property
    def denied_count(self) -> int:
        return self._denied

    def __len__(self) -> int:
        return len(self._records)
