"""Pydantic models for QA scenarios and project policies.

The runner rejects unknown fields, normalised literals, and structurally
invalid input before any browser, network, or model call. Scenarios are the
caller's contract: they declare exact fixtures, expected API behaviour, and
deterministic assertions; the runner does not infer correctness from the
application under test. Policies authorise side effects and disclosure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

# A fixture interpolation placeholder such as ``{{run_id}}`` only accepts
# letters, digits, and underscores. This is the full set the runner recognises
# and replaces before any browser or network call.
_FIXTURE_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_HTTP_METHODS = {"GET", "POST", "PATCH", "DELETE", "PUT", "HEAD", "OPTIONS"}


class _StrictModel(BaseModel):
    """Base model that rejects unknown fields and never coerces inputs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, frozen=True)


# ---------------------------------------------------------------------------
# Policy models
# ---------------------------------------------------------------------------
class OriginRule(_StrictModel):
    """A single origin authorisation. Path and method narrowing are optional."""

    origin: HttpUrl
    methods: frozenset[str] = frozenset({"GET", "HEAD"})
    path_prefix: str = "/"

    @field_validator("origin")
    @classmethod
    def _validate_origin(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.path not in {None, "/"} or value.query or value.fragment:
            raise ValueError("origin must contain only a scheme, host, and optional port")
        return value

    @field_validator("methods")
    @classmethod
    def _validate_methods(cls, value: frozenset[str]) -> frozenset[str]:
        bad = {m for m in value if m.upper() not in _HTTP_METHODS}
        if bad:
            raise ValueError(f"unsupported HTTP methods: {sorted(bad)}")
        return frozenset(m.upper() for m in value)

    @field_validator("path_prefix")
    @classmethod
    def _validate_path_prefix(cls, value: str) -> str:
        candidate = value if value.startswith("/") else f"/{value}"
        if "//" in candidate:
            raise ValueError("path_prefix must not contain '//'")
        return candidate


class NetworkPolicy(_StrictModel):
    """Authorises read and write network activity for a run."""

    allowed_origins: tuple[OriginRule, ...] = Field(default_factory=tuple)
    # Optional capture filter: when set, evidence collection is restricted to
    # these origins. When unset, capture follows allowed_origins.
    capture_origins: tuple[OriginRule, ...] | None = None

    def permits(self, method: str, origin: str, path: str) -> bool:
        if method.upper() not in _HTTP_METHODS:
            return False
        normalized_origin = origin.rstrip("/")
        for rule in self.allowed_origins:
            if str(rule.origin).rstrip("/") != normalized_origin:
                continue
            if method.upper() not in rule.methods:
                continue
            prefix = rule.path_prefix.rstrip("/")
            if prefix and path != prefix and not path.startswith(prefix + "/"):
                continue
            return True
        return False

    def captures(self, origin: str) -> bool:
        rules = self.capture_origins if self.capture_origins is not None else self.allowed_origins
        normalized = origin.rstrip("/")
        return any(str(rule.origin).rstrip("/") == normalized for rule in rules)


class ModelDisclosurePolicy(_StrictModel):
    """Authorises content sent to hosted models and screenshot capture."""

    allow_page_text: bool = False
    allow_action_history: bool = False
    allow_request_bodies: bool = False
    allow_response_bodies: bool = False
    allow_screenshots: bool = False


class IdentityPolicy(_StrictModel):
    """Browser identity and capture configuration."""

    # "isolated" (default) launches a dedicated QA profile; "attach" joins an
    # existing browser profile only when both the CLI flag and this value
    # agree. attach_profile_name is required when mode == "attach".
    mode: Literal["isolated", "attach"] = "isolated"
    attach_profile_name: str | None = None

    @model_validator(mode="after")
    def _validate_attach_profile(self) -> IdentityPolicy:
        if self.mode == "attach" and not self.attach_profile_name:
            raise ValueError("mode='attach' requires attach_profile_name")
        return self


class RedactionPolicy(_StrictModel):
    """Defines what is stripped from stored evidence."""

    redact_headers: tuple[str, ...] = ("authorization", "cookie", "set-cookie", "x-api-key")
    redact_body_fields: tuple[str, ...] = ("password", "token", "secret", "apiKey")


class Policy(_StrictModel):
    """A project policy authorises where the runner may connect, what it may
    send to hosted models, and what stays out of stored evidence."""

    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    model_disclosure: ModelDisclosurePolicy = Field(default_factory=ModelDisclosurePolicy)
    identity: IdentityPolicy = Field(default_factory=IdentityPolicy)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)
    policy_version: str = "1"

    def method_allowed(self, method: str, origin: str, path: str) -> bool:
        return self.network.permits(method, origin, path)


# ---------------------------------------------------------------------------
# Run configuration
# ---------------------------------------------------------------------------
class RunLimits(_StrictModel):
    """Bounded execution: defaults from the design interview."""

    max_actions: int = Field(default=60, ge=1)
    max_seconds: float = Field(default=120.0, gt=0.0)
    model_timeout_seconds: float = Field(default=25.0, gt=0.0)
    navigation_timeout_seconds: float = Field(default=15.0, gt=0.0)
    input_timeout_seconds: float = Field(default=8.0, gt=0.0)


# ---------------------------------------------------------------------------
# Scenario models
# ---------------------------------------------------------------------------
class Selector(_StrictModel):
    """Caller-authored CSS or test-id selector used to read DOM state."""

    css: str | None = None
    testid: str | None = None

    @model_validator(mode="after")
    def _validate_one(self) -> Selector:
        if bool(self.css) == bool(self.testid):
            raise ValueError("Selector requires exactly one of 'css' or 'testid'")
        return self


# Assertion kinds. Each is a code-authorised check; the model never authors
# selectors or fields, only receives the caller's expected behaviour.
class EqualsAssertion(_StrictModel):
    kind: Literal["equals"]
    selector: Selector
    expected: Any


class ContainsAssertion(_StrictModel):
    kind: Literal["contains"]
    selector: Selector
    expected: str | list[Any] | dict[str, Any]


class StatusAssertion(_StrictModel):
    kind: Literal["status"]
    method: Literal["GET", "POST", "PATCH", "DELETE"]
    path: str
    expected_status: int = Field(ge=100, le=599)


class PersistenceAssertion(_StrictModel):
    """Match one record in the independently fetched payload for this path."""

    kind: Literal["persistence"]
    method: Literal["GET"] = "GET"
    path: str
    contains: dict[str, Any]
    absent: bool = False
    records_path: str = "$"

    @field_validator("records_path")
    @classmethod
    def _validate_records_path(cls, value: str) -> str:
        if value != "$" and not re.fullmatch(r"\$(?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[\d+\])*)+", value):
            raise ValueError("records_path must be '$' or a supported JSON field path")
        return value

    @field_validator("contains")
    @classmethod
    def _require_criteria(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("persistence requires nonempty record criteria")
        return value


class NetworkAssertion(_StrictModel):
    """Validate a captured request/response pair."""

    kind: Literal["network"]
    method: str
    path: str
    expected_status: int = Field(ge=100, le=599)
    payload_contains: dict[str, Any] | None = None
    response_contains: dict[str, Any] | None = None
    capture: dict[str, str] = Field(default_factory=dict)

    @field_validator("capture")
    @classmethod
    def _validate_capture(cls, value: dict[str, str]) -> dict[str, str]:
        for name, path in value.items():
            if not _FIXTURE_KEY_RE.fullmatch(name) or name == "run_id":
                raise ValueError("capture names must be fixture identifiers other than run_id")
            if not path.startswith("$."):
                raise ValueError("capture paths must start with '$.'")
        return value


class NoRequestAssertion(_StrictModel):
    kind: Literal["no_request"]
    method: Literal["GET", "POST", "PATCH", "DELETE", "PUT", "HEAD", "OPTIONS"]
    path: str


class CountAssertion(_StrictModel):
    kind: Literal["count"]
    selector: Selector
    expected: int = Field(ge=0, strict=True)


class AttributeAssertion(_StrictModel):
    kind: Literal["attribute"]
    selector: Selector
    name: str = Field(min_length=1)
    expected: str | None


Assertion = (
    EqualsAssertion
    | ContainsAssertion
    | StatusAssertion
    | PersistenceAssertion
    | NetworkAssertion
    | NoRequestAssertion
    | CountAssertion
    | AttributeAssertion
)


class Step(_StrictModel):
    """One UI goal with caller-owned fixtures and deterministic assertions."""

    id: str
    goal: str
    fixtures: Mapping[str, str] = Field(default_factory=dict)
    assertions: tuple[Assertion, ...] = Field(default_factory=tuple)
    # If true, the step should perform a fresh reload-and-read at the end
    # (used by persistence assertions and persistence checks).
    reload_after: bool = False
    scope: Selector | None = None
    skip_if_absent: bool = False

    @model_validator(mode="after")
    def _validate_skip_scope(self) -> Step:
        if self.skip_if_absent and self.scope is None:
            raise ValueError("skip_if_absent requires an explicit scope selector")
        return self

    @field_validator("fixtures")
    @classmethod
    def _validate_fixture_keys(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        for key in value:
            if not _FIXTURE_KEY_RE.match(key):
                raise ValueError(f"fixture key {key!r} must match {_FIXTURE_KEY_RE.pattern}")
        return dict(value)


class Scenario(_StrictModel):
    """Top-level scenario: a list of steps with goals, fixtures, and assertions."""

    scenario_version: Literal["1"] = "1"
    name: str
    description: str | None = None
    mode: Literal["contract", "exploratory"] = "contract"
    run_id: str = Field(default_factory=lambda: str(uuid4()))
    start_url: HttpUrl
    steps: tuple[Step, ...]
    cleanup: tuple[Step, ...] = Field(default_factory=tuple)
    limits: RunLimits = Field(default_factory=RunLimits)

    @field_validator("run_id")
    @classmethod
    def _validate_run_id(cls, value: str) -> str:
        if not _UUID_RE.fullmatch(value) or UUID(value).version != 4:
            raise ValueError("run_id must be a UUID v4 string")
        return value

    @model_validator(mode="after")
    def _require_steps_and_assertions(self) -> Scenario:
        if any(step.scope is None for step in self.cleanup):
            raise ValueError("cleanup steps require an explicit run-owned scope")
        if not self.steps:
            raise ValueError("scenario requires at least one step")
        for step in (*self.steps, *self.cleanup):
            if self.mode == "contract" and not step.assertions:
                raise ValueError(f"step {step.id!r} has no assertions; a contract run must declare expectations")
        return self


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------
class AssertionResult(_StrictModel):
    name: str
    kind: str
    passed: bool
    expected: Any | None = None
    observed: Any | None = None
    detail: str | None = None


class EvidenceRecord(_StrictModel):
    """One captured network exchange. Bodies are optional and gated by policy."""

    method: str
    url: str
    status: int | None = None
    request_headers: Mapping[str, str] = Field(default_factory=dict)
    request_body: Any | None = None
    response_headers: Mapping[str, str] = Field(default_factory=dict)
    response_body: Any | None = None
    error: str | None = None
    incomplete: bool = False


class ActionRecord(_StrictModel):
    step: str
    action_id: str
    label: str
    kind: str
    text: str | None = None
    confidence: float | None = None
    operation: str | None = None
    target: str | None = None
    page_url: str | None = None
    executed_at_ms: int
    latency_ms: int


class StepResult(_StrictModel):
    id: str
    status: Literal["pass", "fail", "blocked", "error", "complete"]
    assertions: tuple[AssertionResult, ...] = Field(default_factory=tuple)
    actions: tuple[ActionRecord, ...] = Field(default_factory=tuple)
    evidence: tuple[EvidenceRecord, ...] = Field(default_factory=tuple)
    note: str | None = None


class Report(_StrictModel):
    """Machine-readable QA report."""

    report_version: Literal["1"] = "1"
    scenario_name: str
    run_id: str
    verdict: Literal["pass", "fail", "blocked", "error", "complete"]
    model_provider: str
    model_used: str | None = None
    started_at_ms: int
    completed_at_ms: int
    steps: tuple[StepResult, ...]
    metadata: dict[str, str] = Field(default_factory=dict)
    execution_mode: Literal["isolated", "attach"] = "isolated"
    findings: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    cleanup_notes: tuple[str, ...] = ()
    note: str | None = None
