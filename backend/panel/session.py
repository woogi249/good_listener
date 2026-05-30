"""실시간 회의 세션 상태와 패널 실행 로직."""
from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime

from .cli_dispatcher import call_claude_with_codex_fallback, enforce_one_line_50chars
from .config import DEFAULT_BUFFER_SIZE, DEFAULT_CLI_TIMEOUT_S, REALTIME_PANELS
from .prompts import render
from .rolling_buffer import RollingBuffer, Utterance
from .triggers import RealtimeTriggerEngine, TriggerEvent


@dataclass
class PanelOutput:
    panel_name: str
    title: str
    text: str
    importance: int
    reason: str
    provider: str
    elapsed_s: float
    updated_at: str
    status: str = "idle"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TranscriptItem:
    index: int
    timestamp: str
    speaker: str
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class MeetingContext:
    topic: str = ""
    goal: str = ""
    terms: list[str] | None = None

    def normalized_terms(self) -> list[str]:
        return list(self.terms or [])

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "goal": self.goal,
            "terms": self.normalized_terms(),
        }

    def as_prompt_block(self) -> str:
        lines: list[str] = []
        if self.topic:
            lines.append(f"- 주제: {self.topic}")
        if self.goal:
            lines.append(f"- 목표: {self.goal}")
        if self.terms:
            lines.append("- 주요 용어: " + ", ".join(self.terms))
        if not lines:
            return ""
        return "## 회의 준비 문맥\n" + "\n".join(lines) + "\n\n"


_PANEL_TITLES = {config.panel_name: config.title for config in REALTIME_PANELS}
_EMPTY_PANEL_TEXT = {
    "summarizer": "요약 대기",
    "fact_checker": "검증 주장 대기",
    "ideator": "아이디어 대기",
    "devils_advocate": "반박 대기",
}


class MeetingSession:
    def __init__(
        self,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        cli_timeout_s: float = DEFAULT_CLI_TIMEOUT_S,
        mock_ai: bool = False,
    ):
        self.buffer = RollingBuffer(maxlen=buffer_size)
        self.trigger_engine = RealtimeTriggerEngine()
        self.cli_timeout_s = cli_timeout_s
        self.mock_ai = mock_ai
        self.running = False
        self.mic_running = False
        self.context = MeetingContext()
        self.enabled_panels = {
            config.panel_name for config in REALTIME_PANELS if config.enabled
        }
        self.transcript: list[TranscriptItem] = []
        self.panels = {
            config.panel_name: PanelOutput(
                panel_name=config.panel_name,
                title=config.title,
                text=_EMPTY_PANEL_TEXT[config.panel_name],
                importance=1,
                reason="",
                provider="",
                elapsed_s=0.0,
                updated_at="",
                status="idle",
            )
            for config in REALTIME_PANELS
        }
        self._lock = threading.RLock()

    def start(self) -> dict:
        with self._lock:
            self.running = True
        return self.state()

    def prepare_context(
        self,
        topic: str = "",
        goal: str = "",
        terms: list[str] | None = None,
    ) -> dict:
        with self._lock:
            self.context = MeetingContext(
                topic=topic.strip(),
                goal=goal.strip(),
                terms=[term.strip() for term in (terms or []) if term.strip()],
            )
        return self.state()

    def stop(self) -> dict:
        with self._lock:
            self.running = False
            self.mic_running = False
        return self.state()

    def reset(self) -> dict:
        with self._lock:
            self.buffer = RollingBuffer(maxlen=DEFAULT_BUFFER_SIZE)
            self.trigger_engine = RealtimeTriggerEngine()
            self.transcript.clear()
            self.context = MeetingContext()
            for name, panel in self.panels.items():
                panel.text = _EMPTY_PANEL_TEXT[name]
                panel.importance = 1
                panel.reason = ""
                panel.provider = ""
                panel.elapsed_s = 0.0
                panel.updated_at = ""
                panel.status = "idle"
        return self.state()

    def set_panel_enabled(self, panel_name: str, enabled: bool) -> dict:
        with self._lock:
            if enabled:
                self.enabled_panels.add(panel_name)
                self.panels[panel_name].status = "idle"
            else:
                self.enabled_panels.discard(panel_name)
                self.panels[panel_name].status = "muted"
        return self.state()

    def set_mic_running(self, running: bool) -> dict:
        with self._lock:
            self.mic_running = running
            if running:
                self.running = True
        return self.state()

    def add_utterance(
        self,
        text: str,
        speaker: str = "unknown",
    ) -> tuple[TranscriptItem | None, list[TriggerEvent]]:
        text = text.strip()
        if not text:
            return None, []

        with self._lock:
            now = datetime.now()
            utterance = Utterance(timestamp=now, speaker=speaker, text=text)
            self.buffer.add(utterance)
            item = TranscriptItem(
                index=len(self.transcript) + 1,
                timestamp=now.isoformat(timespec="seconds"),
                speaker=speaker,
                text=text,
            )
            self.transcript.append(item)
            events = self.trigger_engine.evaluate(
                text=text,
                buffer=self.buffer,
                enabled_panels=set(self.enabled_panels),
            )
            for event in events:
                self.panels[event.panel_name].status = "thinking"
                self.panels[event.panel_name].reason = event.reason
                self.panels[event.panel_name].importance = event.importance
            return item, events

    def run_panel(self, event: TriggerEvent) -> PanelOutput:
        if self.mock_ai:
            text = self._mock_panel_text(event)
            provider = "mock"
            elapsed_s = 0.0
        else:
            prompt = self._render_prompt(event.panel_name)
            response = call_claude_with_codex_fallback(
                prompt,
                timeout_s=self.cli_timeout_s,
            )
            text = enforce_one_line_50chars(response.stdout)
            provider = response.provider
            elapsed_s = response.elapsed_s
            if response.fallback_from:
                provider = f"{provider}:fallback"
            if not response.success:
                text = ""
                provider = response.provider

        with self._lock:
            current = self.panels[event.panel_name]
            if text:
                current.text = text
                current.importance = max(event.importance, self._importance_from_text(text))
            current.provider = provider
            current.elapsed_s = elapsed_s
            current.reason = event.reason
            current.updated_at = datetime.now().isoformat(timespec="seconds")
            current.status = "idle"
            return PanelOutput(**current.as_dict())

    def state(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "mic_running": self.mic_running,
                "enabled_panels": sorted(self.enabled_panels),
                "panels": {
                    name: panel.as_dict()
                    for name, panel in self.panels.items()
                },
                "transcript": [item.as_dict() for item in self.transcript[-40:]],
                "context": self.context.as_dict(),
            }

    def _render_prompt(self, panel_name: str) -> str:
        with self._lock:
            transcript = self.buffer.as_text()
            context = self.context.as_prompt_block()
        return context + render(panel_name, transcript)

    def _importance_from_text(self, text: str) -> int:
        high_markers = ("확인", "리스크", "위험", "결정", "마감", "비용", "보안")
        if any(marker in text for marker in high_markers):
            return 3
        return 1

    def _mock_panel_text(self, event: TriggerEvent) -> str:
        text = event.utterance
        if event.panel_name == "summarizer":
            return enforce_one_line_50chars(f"최근 논의 핵심: {text}")
        if event.panel_name == "fact_checker":
            return enforce_one_line_50chars(f"확인 필요: {text}")
        if event.panel_name == "ideator":
            return enforce_one_line_50chars("대안을 2개로 나눠 비교해볼까요?")
        if event.panel_name == "devils_advocate":
            return enforce_one_line_50chars("리스크: 결정 전 비용·일정 검증 필요")
        return enforce_one_line_50chars(text)
