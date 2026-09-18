"""Meaningful progress and bounded exploration recovery; see THIRD_PARTY_NOTICES.md."""

import json
from collections import Counter, deque
from dataclasses import dataclass, field


def semantic_state(page) -> str:
    controls = page.controls or page.actions
    return json.dumps(
        {
            "url": page.url,
            "text": page.text,
            "scroll": page.scroll,
            "controls": [
                {
                    key: control[key]
                    for key in (
                        "role",
                        "label",
                        "value",
                        "checked",
                        "selected",
                        "pressed",
                        "expanded",
                        "context",
                        "availability",
                        "option_labels",
                        "active_option",
                        "scroll_y",
                    )
                    if key in control
                }
                for control in controls
            ],
        },
        sort_keys=True,
    )


def action_key(page, action: dict) -> str:
    meaning = {
        key: action[key]
        for key in (
            "kind",
            "key",
            "delta",
            "role",
            "label",
            "value",
            "checked",
            "selected",
            "expanded",
            "context",
            "scroll_y",
        )
        if key in action
    }
    peers = []
    label = action.get("control_label", action.get("label"))
    for candidate in page.controls or page.actions:
        if (
            candidate.get("role") == action.get("role")
            and candidate.get("control_label", candidate.get("label")) == label
            and candidate.get("node") not in peers
        ):
            peers.append(candidate.get("node"))
    meaning["ordinal"] = peers.index(action.get("node")) if action.get("node") in peers else 0
    if action.get("kind") == "scroll":
        meaning["scroll_y"] = page.scroll.get("y")
    return json.dumps({"url": page.url, "control": meaning}, sort_keys=True)


@dataclass
class ProgressMemory:
    attempts: Counter = field(default_factory=Counter)
    visits: Counter = field(default_factory=Counter)
    recent: deque = field(default_factory=lambda: deque(maxlen=6))
    unchanged_scrolls: int = 0
    scroll_streak: int = 0
    unchanged_waits: int = 0
    checkpoints: int = 0

    def exhausted(self, page, action: dict) -> bool:
        return self.attempts[action_key(page, action)] >= 2

    def record(self, before, action: dict, after) -> str | None:
        key = action_key(before, action)
        self.attempts[key] += 1
        changed = semantic_state(before) != semantic_state(after)
        self.recent.append((before.url, action.get("kind"), action.get("label"), changed))
        kind = action.get("kind")
        if kind in {"scroll", "scroll_element"}:
            self.scroll_streak += 1
            self.unchanged_scrolls = 0 if before.text != after.text else self.unchanged_scrolls + 1
            if self.unchanged_scrolls >= 3 or self.scroll_streak >= 4:
                return "Scroll checkpoint: read current content before choosing a different subgoal"
        elif kind != "wait":
            self.unchanged_scrolls = self.scroll_streak = 0
        self.unchanged_waits = self.unchanged_waits + 1 if kind == "wait" and not changed else 0
        if self.unchanged_waits >= 2:
            return "Wait checkpoint: two waits produced no meaningful change"
        if len(self.recent) >= 3 and all(not event[3] for event in list(self.recent)[-3:]):
            return "Progress checkpoint: three actions produced no meaningful change"
        if len(self.recent) == 6:
            events = list(self.recent)
            if all(event[:3] == events[index % 2][:3] for index, event in enumerate(events)):
                return "Progress checkpoint: alternating action cycle detected"
        if kind != "wait":
            state = semantic_state(after)
            self.visits[state] += 1
            if self.visits[state] >= 3:
                return "Progress checkpoint: the same meaningful page state returned three times"
        return None
