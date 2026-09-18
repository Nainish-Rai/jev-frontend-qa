"""Snapshot execution and freshness markers.

Adapted from the upstream snapshot.js. The runner uses the snapshot to
identify observed actions, read the visible text, and compute a stable
marker that detects page changes between a decision and the browser-side
input. Geometry is excluded so the marker survives layout shifts; the
executor hits the live geometry immediately before dispatching input.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .browser import BrowserTransport

SNAPSHOT_JS_PATH = Path(__file__).with_name("snapshot.js")


def _fingerprint(state: dict) -> str:
    content = {k: state.get(k) for k in ("url", "text", "scroll", "page_key")}
    content["actions"] = [
        {key: value for key, value in action.items() if key != "rect"} for action in state.get("actions", ())
    ]
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class PageState:
    url: str
    title: str
    text: str
    actions: tuple[dict, ...]
    scroll: dict
    fingerprint: str
    page_key: list
    guards: dict
    width: int
    height: int
    omitted_actions: int = 0
    truncated_text: bool = False
    unsupported: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def marker(self) -> str:
        return self.fingerprint


def read_snapshot(transport: BrowserTransport) -> PageState | None:
    """Run the snapshot script in the owned session. Returns None while navigating."""

    js = SNAPSHOT_JS_PATH.read_text()
    result = transport.evaluate_js(js)
    if not isinstance(result, dict):
        return None
    actions = result.get("actions") or []
    return PageState(
        url=result["url"],
        title=result["title"],
        text=result.get("text", ""),
        actions=tuple(actions),
        scroll=result.get("scroll", {"y": 0, "height": 0}),
        fingerprint=_fingerprint(result),
        page_key=result.get("page_key", []),
        guards=result.get("guards", {}),
        width=result.get("w", 0),
        height=result.get("h", 0),
        omitted_actions=result.get("omitted_actions", 0),
        truncated_text=result.get("truncated_text", False),
        unsupported=result.get("unsupported", False),
        metadata=result.get("metadata", {}),
    )
