"""전역 LLM 예산 게이트."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BudgetConfig:
    enabled: bool = True
    max_budget_krw: float = 1000.0
    max_calls_per_meeting: int = 100
    max_calls_per_minute: int = 6
    usd_to_krw: float = 1500.0
    input_usd_per_mtok: float = 0.2
    output_usd_per_mtok: float = 0.8
    estimated_output_tokens: int = 128
    min_estimated_call_krw: float = 2.0


@dataclass(frozen=True)
class BudgetReservation:
    feature: str
    estimated_cost_krw: float
    estimated_input_tokens: int
    estimated_output_tokens: int
    reserved_at: float


@dataclass(frozen=True)
class BudgetDecision:
    allowed: bool
    reason: str
    reservation: BudgetReservation | None = None


class BudgetGate:
    """회의 단위 LLM 비용과 호출량을 제한한다."""

    def __init__(self, config: BudgetConfig | None = None):
        self._config = config or BudgetConfig()
        self._lock = threading.RLock()
        self._used_krw = 0.0
        self._call_count = 0
        self._blocked_count = 0
        self._call_times: list[float] = []
        self._last_block_reason = ""

    @property
    def config(self) -> BudgetConfig:
        return self._config

    def configure(self, config: BudgetConfig) -> None:
        with self._lock:
            self._config = config
            self.reset()

    def reset(self) -> None:
        with self._lock:
            self._used_krw = 0.0
            self._call_count = 0
            self._blocked_count = 0
            self._call_times.clear()
            self._last_block_reason = ""

    def reserve_messages(
        self,
        messages: list[dict[str, Any]],
        feature: str,
        now: float | None = None,
    ) -> BudgetDecision:
        config = self._config
        if not config.enabled:
            return BudgetDecision(allowed=True, reason="budget gate disabled")

        current = time.monotonic() if now is None else now
        input_tokens = estimate_messages_tokens(messages)
        output_tokens = max(1, config.estimated_output_tokens)
        estimated = estimate_cost_krw(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            config=config,
        )

        with self._lock:
            self._prune_calls(current)
            reason = self._block_reason(estimated)
            if reason:
                self._blocked_count += 1
                self._last_block_reason = reason
                return BudgetDecision(allowed=False, reason=reason)

            self._used_krw += estimated
            self._call_count += 1
            self._call_times.append(current)
            reservation = BudgetReservation(
                feature=feature,
                estimated_cost_krw=estimated,
                estimated_input_tokens=input_tokens,
                estimated_output_tokens=output_tokens,
                reserved_at=current,
            )
            return BudgetDecision(allowed=True, reason="reserved", reservation=reservation)

    def settle(
        self,
        reservation: BudgetReservation | None,
        usage: dict[str, int] | None,
    ) -> None:
        if reservation is None or not self._config.enabled or not usage:
            return

        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        if input_tokens <= 0 and output_tokens <= 0:
            return

        actual = estimate_cost_krw(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            config=self._config,
            apply_minimum=False,
        )
        with self._lock:
            self._used_krw = max(0.0, self._used_krw - reservation.estimated_cost_krw + actual)

    def state(self) -> dict:
        config = self._config
        with self._lock:
            remaining = max(0.0, config.max_budget_krw - self._used_krw)
            status = "disabled"
            if config.enabled:
                if remaining <= 0 or self._call_count >= config.max_calls_per_meeting:
                    status = "exhausted"
                elif self._last_block_reason:
                    status = "limited"
                else:
                    status = "ok"
            return {
                "enabled": config.enabled,
                "cap_krw": round(config.max_budget_krw, 2),
                "used_krw": round(self._used_krw, 2),
                "remaining_krw": round(remaining, 2),
                "call_count": self._call_count,
                "blocked_count": self._blocked_count,
                "max_calls_per_meeting": config.max_calls_per_meeting,
                "max_calls_per_minute": config.max_calls_per_minute,
                "status": status,
                "last_block_reason": self._last_block_reason,
            }

    def _block_reason(self, estimated_cost_krw: float) -> str:
        config = self._config
        if self._call_count >= config.max_calls_per_meeting:
            return "회의당 LLM 호출 한도 초과"
        if len(self._call_times) >= config.max_calls_per_minute:
            return "분당 LLM 호출 한도 초과"
        if self._used_krw + estimated_cost_krw > config.max_budget_krw:
            return "회의 예산 한도 초과"
        return ""

    def _prune_calls(self, now: float) -> None:
        cutoff = now - 60.0
        self._call_times = [called_at for called_at in self._call_times if called_at >= cutoff]


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_text_tokens(str(message.get("role", "")))
        total += estimate_text_tokens(str(message.get("content", "")))
    return max(1, total)


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 2))


def estimate_cost_krw(
    input_tokens: int,
    output_tokens: int,
    config: BudgetConfig,
    apply_minimum: bool = True,
) -> float:
    input_usd = (max(0, input_tokens) / 1_000_000) * config.input_usd_per_mtok
    output_usd = (max(0, output_tokens) / 1_000_000) * config.output_usd_per_mtok
    cost = (input_usd + output_usd) * config.usd_to_krw
    if apply_minimum:
        cost = max(cost, config.min_estimated_call_krw)
    return round(cost, 6)
