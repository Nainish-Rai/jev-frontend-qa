"""Caller-authored checks; model completion is never correctness evidence."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .browser import BrowserTransport
from .evidence import EvidenceRecord
from .models import (
    Assertion,
    AttributeAssertion,
    ContainsAssertion,
    CountAssertion,
    EqualsAssertion,
    NetworkAssertion,
    NoRequestAssertion,
    PersistenceAssertion,
    Policy,
    Selector,
    StatusAssertion,
)
from .redaction import redact_body


@dataclass
class AssertionOutcome:
    name: str
    kind: str
    passed: bool
    expected: Any = None
    observed: Any = None
    detail: str | None = None


def evaluate_step(
    *,
    step_id: str,
    step_goal: str,
    assertions: Sequence[Assertion],
    evidence: Sequence[EvidenceRecord],
    fresh_page_payloads: dict[str, Any],
    transport: BrowserTransport | None,
    policy: Policy,
    evidence_complete: bool | None = None,
) -> list[AssertionOutcome]:
    """Evaluate a settled step window, including explicit negative assertions.

    The runner must explicitly attest a settled, lossless window with True to
    prove no_request. False invalidates every result; an empty buffer is not proof.
    """
    complete = evidence_complete is not False and not any(record.incomplete or record.error for record in evidence)
    outcomes = []
    for index, assertion in enumerate(assertions):
        name = f"{step_id}.{index}.{assertion.kind}"
        if not complete or (isinstance(assertion, NoRequestAssertion) and evidence_complete is not True):
            outcomes.append(
                AssertionOutcome(
                    name=name,
                    kind=assertion.kind,
                    passed=False,
                    detail="step evidence is incomplete; absence and success cannot be certified",
                )
            )
            continue
        outcomes.append(
            _evaluate_one(
                name=name,
                assertion=assertion,
                evidence=evidence,
                fresh_page_payloads=fresh_page_payloads,
                transport=transport,
                policy=policy,
            )
        )
    return outcomes


def _evaluate_one(
    *,
    name: str,
    assertion: Assertion,
    evidence: Sequence[EvidenceRecord],
    fresh_page_payloads: dict[str, Any],
    transport: BrowserTransport | None,
    policy: Policy,
) -> AssertionOutcome:
    if isinstance(assertion, (EqualsAssertion, ContainsAssertion, CountAssertion, AttributeAssertion)):
        return _evaluate_value(name, assertion, transport)
    if isinstance(assertion, PersistenceAssertion):
        return _evaluate_persistence(name, assertion, fresh_page_payloads, policy)
    candidates = matching_exchanges(evidence, method=assertion.method, path=assertion.path)
    if isinstance(assertion, NoRequestAssertion):
        return AssertionOutcome(
            name=name,
            kind=assertion.kind,
            passed=not candidates,
            expected=0,
            observed=len(candidates),
            detail="unexpected request observed" if candidates else None,
        )
    if len(candidates) != 1:
        return AssertionOutcome(
            name=name,
            kind=assertion.kind,
            passed=False,
            expected=assertion.expected_status,
            observed=len(candidates),
            detail=f"expected one captured {assertion.method} {assertion.path}; observed {len(candidates)}",
        )
    candidate = candidates[0]
    if isinstance(assertion, StatusAssertion):
        passed = not candidate.error and not candidate.incomplete and candidate.status == assertion.expected_status
        return AssertionOutcome(
            name=name,
            kind=assertion.kind,
            passed=passed,
            expected=assertion.expected_status,
            observed=candidate.status,
            detail=None if passed else "captured exchange did not satisfy expected status",
        )
    failures = _network_failures(assertion, candidate)
    return AssertionOutcome(
        name=name,
        kind=assertion.kind,
        passed=not failures,
        expected=assertion.model_dump(),
        observed={
            "status": candidate.status,
            "payload": redact_body(candidate.request_body, policy.redaction),
            "response": redact_body(candidate.response_body, policy.redaction),
        },
        detail="; ".join(failures) if failures else None,
    )


def matching_exchanges(
    evidence: Sequence[EvidenceRecord],
    *,
    method: str,
    path: str,
) -> list[EvidenceRecord]:
    # Endpoint identity is exact. /todos/1 must never match /todos/10.
    return [
        record
        for record in evidence
        if record.method.upper() == method.upper() and (urlsplit(record.url).path or "/") == path
    ]


def _network_failures(assertion: NetworkAssertion, candidate: EvidenceRecord) -> list[str]:
    failures = []
    if candidate.error or candidate.incomplete or candidate.status is None:
        failures.append("captured exchange is incomplete or failed")
    if candidate.status != assertion.expected_status:
        failures.append(f"expected status {assertion.expected_status}, observed {candidate.status}")
    if assertion.payload_contains is not None and not _matches_expected(
        candidate.request_body, assertion.payload_contains
    ):
        failures.append("request payload does not match all expected fields in the same object")
    if assertion.response_contains is not None and not _matches_expected(
        candidate.response_body, assertion.response_contains
    ):
        failures.append("response body does not match all expected fields in the same object")
    return failures


def capture_network_variables(
    assertions: Sequence[Assertion],
    evidence: Sequence[EvidenceRecord],
) -> dict[str, str]:
    """Capture identifiers only from uniquely identified, verified exchanges.

    The runner renders network assertions first, calls this helper, then renders
    dependent DOM/persistence assertions. Captures never replace caller fixtures.
    """
    captured: dict[str, str] = {}
    for assertion in assertions:
        if not isinstance(assertion, NetworkAssertion) or not assertion.capture:
            continue
        candidates = matching_exchanges(evidence, method=assertion.method, path=assertion.path)
        if len(candidates) != 1 or _network_failures(assertion, candidates[0]):
            raise ValueError("capture requires exactly one complete exchange satisfying its network contract")
        for name, path in assertion.capture.items():
            value = _jsonpath_lookup(candidates[0].response_body, path)
            if (
                not isinstance(value, (str, int))
                or isinstance(value, bool)
                or not re.fullmatch(r"[A-Za-z0-9_-]+", str(value))
            ):
                raise ValueError(f"capture {name!r} requires a nonempty safe record identifier at {path!r}")
            if name in captured and captured[name] != str(value):
                raise ValueError(f"capture {name!r} is ambiguous")
            captured[name] = str(value)
    return captured


def _evaluate_value(
    name: str,
    assertion: EqualsAssertion | ContainsAssertion | CountAssertion | AttributeAssertion,
    transport: BrowserTransport | None,
) -> AssertionOutcome:
    if transport is None:
        return AssertionOutcome(name, assertion.kind, False, detail="no DOM transport")
    values = _read_dom_values(
        transport, assertion.selector, assertion.name if isinstance(assertion, AttributeAssertion) else None
    )
    if not isinstance(values, list):
        return AssertionOutcome(name, assertion.kind, False, detail="DOM observation unavailable")
    if isinstance(assertion, CountAssertion):
        observed = len(values)
    elif len(values) != 1:
        return AssertionOutcome(
            name,
            assertion.kind,
            False,
            expected=assertion.expected,
            observed=values,
            detail="value selector must identify exactly one element; use count for absence",
        )
    else:
        observed = values[0]
    if isinstance(assertion, ContainsAssertion):
        passed = _container_contains(observed, assertion.expected)
    else:
        passed = _exact_equal(observed, assertion.expected)
    return AssertionOutcome(name, assertion.kind, passed, assertion.expected, observed)


def _evaluate_persistence(
    name: str,
    assertion: PersistenceAssertion,
    fresh_payloads: dict[str, Any],
    policy: Policy,
) -> AssertionOutcome:
    payload = fresh_payloads.get(assertion.path)
    records = _jsonpath_lookup(payload, assertion.records_path)
    valid = isinstance(records, Mapping) or (
        isinstance(records, list) and all(isinstance(row, Mapping) for row in records)
    )
    # Absence needs a successfully fetched collection, not a null/malformed object.
    if not valid or (assertion.absent and not isinstance(records, list)):
        return AssertionOutcome(
            name,
            assertion.kind,
            False,
            expected=assertion.contains,
            observed=redact_body(payload, policy.redaction),
            detail="no fresh record collection at the exact requested path and records_path",
        )
    found = _find_matching_record(records, assertion.contains) is not None
    passed = not found if assertion.absent else found
    return AssertionOutcome(
        name,
        assertion.kind,
        passed,
        expected={"contains": assertion.contains, "absent": assertion.absent},
        observed=redact_body(records, policy.redaction),
        detail=None
        if passed
        else (
            "forbidden persisted record exists"
            if assertion.absent
            else "no single persisted record matched every expected field"
        ),
    )


def _find_matching_record(payload: Any, criteria: dict[str, Any]) -> Any | None:
    rows = payload if isinstance(payload, list) else [payload]
    return next((row for row in rows if isinstance(row, Mapping) and _record_matches(row, criteria)), None)


def _record_matches(record: Mapping[str, Any], criteria: Mapping[str, Any]) -> bool:
    return bool(criteria) and _matches_expected(record, criteria)


def _exact_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) != isinstance(expected, bool):
        return False
    return actual == expected


def _matches_expected(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _matches_expected(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(_matches_expected(left, right) for left, right in zip(actual, expected))
        )
    return _exact_equal(actual, expected)


def _container_contains(container: Any, value: Any) -> bool:
    if isinstance(container, str) and isinstance(value, str):
        return value in container
    if isinstance(container, Mapping) and isinstance(value, Mapping):
        return _matches_expected(container, value)
    if isinstance(container, list):
        return any(_matches_expected(item, value) for item in container)
    return False


def _read_dom_values(transport: BrowserTransport, selector: Selector, attribute: str | None) -> Any:
    selection = (
        f"document.querySelectorAll({json.dumps(selector.css)})"
        if selector.css
        else "Array.from(document.querySelectorAll('[data-testid]')).filter(e=>"
        f"e.getAttribute('data-testid')==={json.dumps(selector.testid)})"
    )
    value = f"e.getAttribute({json.dumps(attribute)})" if attribute is not None else "e.textContent"
    return transport.evaluate_js(f"(()=>{{return Array.from({selection}, e=>{value});}})()")


def _jsonpath_lookup(payload: Any, path: str) -> Any:
    """Small JSONPath subset: $, $.field, $.field[0].nested; missing is None."""
    if path == "$":
        return payload
    if not re.fullmatch(r"\$(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])*)+", path):
        return None
    cursor = payload
    for token in path[2:].split("."):
        field = token.split("[", 1)[0]
        if not isinstance(cursor, Mapping) or field not in cursor:
            return None
        cursor = cursor[field]
        for index in re.findall(r"\[(\d+)\]", token):
            if not isinstance(cursor, list) or int(index) >= len(cursor):
                return None
            cursor = cursor[int(index)]
    return cursor
