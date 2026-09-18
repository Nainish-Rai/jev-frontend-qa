"""Conservative per-attempt accounting; missing billing data is never zero."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import JEV_INPUT_USD_PER_MILLION, JEV_OUTPUT_USD_PER_MILLION, JevUsage


@dataclass
class UsageRecorder:
    requests: int = 0
    failed_requests: int = 0
    latency_ms: int = 0
    _input: int | None = 0
    _output: int | None = 0
    _cost: float | None = 0.0

    def record(self, *, latency_ms: int, failed: bool, usage: Any) -> None:
        self.requests += 1
        self.failed_requests += int(failed)
        self.latency_ms += max(0, latency_ms)
        values = usage if isinstance(usage, Mapping) else {}
        incoming = values.get("input_tokens")
        outgoing = values.get("output_tokens")
        valid_tokens = all(type(value) is int and value >= 0 for value in (incoming, outgoing))
        if "total_tokens" in values:
            total = values["total_tokens"]
            valid_tokens = valid_tokens and type(total) is int and total == incoming + outgoing
        if not valid_tokens:
            self._input = self._output = None
        elif self._input is not None and self._output is not None:
            self._input += incoming
            self._output += outgoing
        # No published token-price assumptions. Only an explicit USD field counts.
        cost = values.get("cost_usd")
        try:
            valid_cost = type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
            if valid_cost and self._cost is not None:
                summed = self._cost + cost
                self._cost = summed if math.isfinite(summed) else None
            else:
                self._cost = None
        except OverflowError:
            self._cost = None

    def summary(self) -> JevUsage:
        tokens_known = self.requests > 0 and self._input is not None and self._output is not None
        cost_known = self.requests > 0 and self._cost is not None
        return JevUsage(
            requests=self.requests,
            failed_requests=self.failed_requests,
            latency_ms=self.latency_ms,
            input_tokens=self._input if tokens_known else None,
            output_tokens=self._output if tokens_known else None,
            total_tokens=self._input + self._output if tokens_known else None,
            cost_usd=self._cost if cost_known else None,
            estimated_cost_usd=(
                (self._input * JEV_INPUT_USD_PER_MILLION + self._output * JEV_OUTPUT_USD_PER_MILLION) / 1_000_000
                if tokens_known
                else None
            ),
            usage_complete=tokens_known,
            cost_complete=cost_known,
        )
