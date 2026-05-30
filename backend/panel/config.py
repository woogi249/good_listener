"""패널별 트리거 설정."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TriggerConfig:
    panel_name: str
    title: str
    utterance_threshold: int   # 누적 N 발화마다 트리거
    cooldown_s: float
    priority: str              # "HIGH" | "MED" | "LOW"
    enabled: bool


SUMMARIZER = TriggerConfig(
    panel_name="summarizer",
    title="A 요약",
    utterance_threshold=10,
    cooldown_s=60.0,
    priority="HIGH",
    enabled=True,
)

FACT_CHECKER = TriggerConfig(
    panel_name="fact_checker",
    title="B 팩트체크",
    utterance_threshold=0,
    cooldown_s=30.0,
    priority="HIGH",
    enabled=True,
)

IDEATOR = TriggerConfig(
    panel_name="ideator",
    title="C 아이디어",
    utterance_threshold=0,
    cooldown_s=120.0,
    priority="LOW",
    enabled=True,
)

DEVILS_ADVOCATE = TriggerConfig(
    panel_name="devils_advocate",
    title="D 반박",
    utterance_threshold=0,
    cooldown_s=90.0,
    priority="MED",
    enabled=True,
)

REALTIME_PANELS = (
    SUMMARIZER,
    FACT_CHECKER,
    IDEATOR,
    DEVILS_ADVOCATE,
)


DEFAULT_BUFFER_SIZE = 10
DEFAULT_CLI_TIMEOUT_S = 15.0
DEFAULT_STT_CHUNK_S = 4
