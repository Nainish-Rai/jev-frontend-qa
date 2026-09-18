"""Decision-provider seam.

The agent loop depends on :class:`DecisionProvider`, not on the HTTP client.
The CLI wires the real TypeSafe-backed provider; tests inject scripted
sequences. The CLI never installs a "pretend model" mode at runtime - the
provider the runner actually uses is reported in the verdict.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .model_client import (
    Decision,
    ModelClient,
    action_space,
    operation_choices,
)


@dataclass
class DecisionProvider:
    """Abstract decision provider. Subclass to inject scripted sequences."""

    name: str

    def choose(
        self,
        *,
        goal: str,
        page: Mapping[str, Any],
        history: Sequence[Mapping[str, Any]],
    ) -> Decision:
        raise NotImplementedError


@dataclass
class TypeSafeDecisionProvider(DecisionProvider):
    """Real TypeSafe / Jev provider."""

    client: ModelClient

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"typesafe:{self.client.model}"

    def choose(self, *, goal: str, page: Mapping[str, Any], history: Sequence[Mapping[str, Any]]) -> Decision:
        elements, targets, controls = action_space(page.get("actions") or [])
        operations = operation_choices(targets, controls)
        return self.client.choose(
            goal=goal,
            page=page,
            history=list(history),
            elements=elements,
            operations=operations,
            targets=targets,
        )


def provider_label(provider: DecisionProvider) -> str:
    return getattr(provider, "name", provider.__class__.__name__)


def model_name_from_provider(provider: DecisionProvider) -> str | None:
    if isinstance(provider, TypeSafeDecisionProvider):
        return provider.client.model
    return None
