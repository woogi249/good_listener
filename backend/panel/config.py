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
    surface: str = "feed"      # "panel" | "feed"


SUMMARIZER = TriggerConfig(
    panel_name="summarizer",
    title="A 요약",
    utterance_threshold=10,
    cooldown_s=60.0,
    priority="HIGH",
    enabled=True,
    surface="panel",
)

FACT_CHECKER = TriggerConfig(
    panel_name="fact_checker",
    title="B 팩트체크",
    utterance_threshold=0,
    cooldown_s=30.0,
    priority="HIGH",
    enabled=True,
    surface="panel",
)

IDEATOR = TriggerConfig(
    panel_name="ideator",
    title="C 아이디어",
    utterance_threshold=0,
    cooldown_s=45.0,
    priority="LOW",
    enabled=True,
    surface="feed",
)

DEVILS_ADVOCATE = TriggerConfig(
    panel_name="devils_advocate",
    title="D 반박",
    utterance_threshold=0,
    cooldown_s=45.0,
    priority="MED",
    enabled=True,
    surface="feed",
)

PARKING_LOT = TriggerConfig(
    panel_name="parking_lot",
    title="E 미해결",
    utterance_threshold=0,
    cooldown_s=20.0,
    priority="MED",
    enabled=True,
    surface="feed",
)

ACTION_CANDIDATE = TriggerConfig(
    panel_name="action_candidate",
    title="TODO 액션 후보",
    utterance_threshold=0,
    cooldown_s=20.0,
    priority="HIGH",
    enabled=True,
    surface="feed",
)

CUSTOMER_PERSPECTIVE = TriggerConfig(
    panel_name="customer_perspective",
    title="고객관점",
    utterance_threshold=0,
    cooldown_s=30.0,
    priority="LOW",
    enabled=True,
    surface="feed",
)

REALTIME_PANELS = (
    SUMMARIZER,
    FACT_CHECKER,
    IDEATOR,
    DEVILS_ADVOCATE,
    PARKING_LOT,
    ACTION_CANDIDATE,
    CUSTOMER_PERSPECTIVE,
)

PRIMARY_PANELS = (
    SUMMARIZER,
    FACT_CHECKER,
)

FEED_PANELS = (
    IDEATOR,
    DEVILS_ADVOCATE,
    PARKING_LOT,
    ACTION_CANDIDATE,
    CUSTOMER_PERSPECTIVE,
)


DEFAULT_BUFFER_SIZE = 10
DEFAULT_CLI_TIMEOUT_S = 15.0
DEFAULT_STT_CHUNK_S = 4
