"""실시간 회의 세션 상태와 패널 실행 로직."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .cli_dispatcher import call_claude_with_codex_fallback, enforce_one_line_50chars
from .config import DEFAULT_BUFFER_SIZE, DEFAULT_CLI_TIMEOUT_S, REALTIME_PANELS, PRIMARY_PANELS
from .exaone_dispatcher import call_exaone, call_exaone_ui_director, call_exaone_web_fact_check
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
    sources: list[dict] = field(default_factory=list)
    tone: str = "neutral"
    emphasis: str = "none"
    density: str = "normal"
    visual_spec: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class InsightFeedItem:
    id: str
    panel_name: str
    label: str
    text: str
    importance: int
    urgency: int
    card_variant: str
    tone: str
    badges: list[str]
    visual_spec: dict
    reason: str
    provider: str
    elapsed_s: float
    updated_at: str
    status: str = "thinking"
    detail_title: str = ""
    detail_body: str = ""
    detail_points: list[str] = field(default_factory=list)
    detail_action: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class LayoutState:
    mode: str = "normal"
    reason: str = ""
    arbiter_enabled: bool = True
    updated_at: str = ""
    ggui_spec: dict = field(default_factory=dict)
    source: str = "local"

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
_PANEL_SURFACES = {config.panel_name: config.surface for config in REALTIME_PANELS}
_EMPTY_PANEL_TEXT = {
    "summarizer": "요약 대기",
    "fact_checker": "검증 주장 대기",
}
_PRIMARY_PANEL_NAMES = {config.panel_name for config in PRIMARY_PANELS}
_MAX_FEED_ITEMS = 80
_DEFAULT_FIXTURE_DELAY_S = 0.9
_LAYOUT_COLUMNS = {
    "normal": [0.75, 0.75, 1.0],
    "expanded": [0.58, 0.58, 1.34],
    "critical": [0.42, 0.42, 1.66],
    "focus_a": [1.25, 0.58, 0.82],
    "focus_b": [0.58, 1.25, 0.82],
    "focus_c": [0.55, 0.55, 1.5],
}
_PRIMARY_COMPONENTS = {
    "summarizer": "summary-panel",
    "fact_checker": "fact-panel",
}
_UI_DIRECTOR_TIMEOUT_S = 6.0
_TONES = {"neutral", "danger", "action", "customer", "opportunity", "pending"}
_EMPHASIS = {"none", "subtle", "strong", "pulse"}
_DENSITIES = {"compact", "normal", "expanded"}


class MeetingSession:
    def __init__(
        self,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        cli_timeout_s: float = DEFAULT_CLI_TIMEOUT_S,
        mock_ai: bool = False,
        ai_provider: str = "exaone",
    ):
        self.buffer = RollingBuffer(maxlen=buffer_size)
        self.trigger_engine = RealtimeTriggerEngine()
        self.cli_timeout_s = cli_timeout_s
        self.mock_ai = mock_ai
        self.ai_provider = self._normalize_ai_provider(ai_provider)
        self.running = False
        self.mic_running = False
        self.context = MeetingContext()
        self.enabled_panels = {
            config.panel_name for config in REALTIME_PANELS if config.enabled
        }
        self.panel_configs = [asdict(config) for config in REALTIME_PANELS]
        self.transcript: list[TranscriptItem] = []
        self.panels = {}
        for config in PRIMARY_PANELS:
            visual_spec = self._primary_visual_spec(
                panel_name=config.panel_name,
                importance=1,
                text=_EMPTY_PANEL_TEXT[config.panel_name],
            )
            self.panels[config.panel_name] = PanelOutput(
                panel_name=config.panel_name,
                title=config.title,
                text=_EMPTY_PANEL_TEXT[config.panel_name],
                importance=1,
                reason="",
                provider="",
                elapsed_s=0.0,
                updated_at="",
                status="idle",
                tone=visual_spec["tone"],
                emphasis=visual_spec["emphasis"],
                density=visual_spec["density"],
                visual_spec=visual_spec,
            )
        self.feed: list[InsightFeedItem] = []
        self.layout = LayoutState()
        self.layout.ggui_spec = self._local_layout_spec("normal", "")
        self._feed_seq = 0
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
            self.running = False
            self.mic_running = False
            self.buffer = RollingBuffer(maxlen=DEFAULT_BUFFER_SIZE)
            self.trigger_engine = RealtimeTriggerEngine()
            self.transcript.clear()
            self.feed.clear()
            self._feed_seq = 0
            self.layout = LayoutState(arbiter_enabled=True)
            self.layout.ggui_spec = self._local_layout_spec("normal", "")
            self.context = MeetingContext()
            self.ai_provider = "exaone"
            self.enabled_panels = {
                config.panel_name for config in REALTIME_PANELS if config.enabled
            }
            for name, panel in self.panels.items():
                visual_spec = self._primary_visual_spec(
                    panel_name=name,
                    importance=1,
                    text=_EMPTY_PANEL_TEXT[name],
                )
                panel.text = _EMPTY_PANEL_TEXT[name]
                panel.importance = 1
                panel.reason = ""
                panel.provider = ""
                panel.elapsed_s = 0.0
                panel.updated_at = ""
                panel.status = "idle"
                panel.sources = []
                self._apply_panel_visual_spec(panel, visual_spec)
        return self.state()

    def set_panel_enabled(self, panel_name: str, enabled: bool) -> dict:
        with self._lock:
            if enabled:
                self.enabled_panels.add(panel_name)
                if panel_name in self.panels:
                    self.panels[panel_name].status = "idle"
            else:
                self.enabled_panels.discard(panel_name)
                if panel_name in self.panels:
                    self.panels[panel_name].status = "muted"
        return self.state()

    def set_mic_running(self, running: bool) -> dict:
        with self._lock:
            self.mic_running = running
            if running:
                self.running = True
        return self.state()

    def set_ai_provider(self, provider: str) -> dict:
        with self._lock:
            self.ai_provider = self._normalize_ai_provider(provider)
        return self.state()

    def set_layout_arbiter_enabled(self, enabled: bool) -> dict:
        with self._lock:
            self.layout.arbiter_enabled = enabled
            if not enabled:
                self.layout.mode = "normal"
                self.layout.reason = ""
                self.layout.ggui_spec = self._local_layout_spec("normal", "")
                self.layout.source = "local"
                self.layout.updated_at = datetime.now().isoformat(timespec="seconds")
        return self.state()

    def add_utterance(
        self,
        text: str,
        speaker: str = "unknown",
        fixture_outputs: dict | None = None,
        fixture_fallback: bool = False,
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
                fixture_payload = self._fixture_payload_for_panel(
                    fixture_outputs,
                    event.panel_name,
                )
                fixture_text = fixture_payload["text"]
                fixture_delay_s = fixture_payload["delay_s"]
                if fixture_fallback and fixture_text is None:
                    fixture_text = self._mock_panel_text(event)
                    fixture_delay_s = _DEFAULT_FIXTURE_DELAY_S
                event.fixture_text = fixture_text
                event.fixture_delay_s = fixture_delay_s
                event.fixture_detail_title = fixture_payload["detail_title"]
                event.fixture_detail_body = fixture_payload["detail_body"]
                event.fixture_detail_points = fixture_payload["detail_points"]
                event.fixture_detail_action = fixture_payload["detail_action"]
                if self._is_primary_panel(event.panel_name):
                    visual_spec = self._primary_visual_spec(
                        panel_name=event.panel_name,
                        importance=event.importance,
                        text=event.utterance,
                        status="thinking",
                    )
                    self.panels[event.panel_name].text = self._thinking_text(event)
                    self.panels[event.panel_name].status = "thinking"
                    self.panels[event.panel_name].reason = event.reason
                    self.panels[event.panel_name].importance = event.importance
                    self._apply_panel_visual_spec(self.panels[event.panel_name], visual_spec)
                    self._update_layout(event.panel_name, event.importance, event.reason)
                    self.panels[event.panel_name].provider = ""
                    self.panels[event.panel_name].elapsed_s = 0.0
                else:
                    feed_item = self._create_feed_item(event)
                    event.output_id = feed_item.id
                    self.feed.append(feed_item)
                    self.feed = self.feed[-_MAX_FEED_ITEMS:]
            return item, events

    def run_panel(self, event: TriggerEvent) -> PanelOutput | InsightFeedItem:
        sources: list[dict] = []
        used_fixture = event.fixture_text is not None
        if event.fixture_text is not None:
            delay_s = _DEFAULT_FIXTURE_DELAY_S if event.fixture_delay_s is None else event.fixture_delay_s
            time.sleep(max(0.0, delay_s))
            text = enforce_one_line_50chars(event.fixture_text)
            provider = "fixture"
            elapsed_s = max(0.0, delay_s)
        elif self.mock_ai:
            text = self._mock_panel_text(event)
            provider = "mock"
            elapsed_s = 0.0
        else:
            prompt = self._render_prompt(event.panel_name)
            if self.ai_provider == "exaone":
                if event.panel_name == "fact_checker":
                    response = call_exaone_web_fact_check(
                        prompt,
                        timeout_s=self.cli_timeout_s,
                    )
                else:
                    response = call_exaone(
                        event.panel_name,
                        prompt,
                        timeout_s=self.cli_timeout_s,
                    )
            else:
                response = call_claude_with_codex_fallback(
                    prompt,
                    timeout_s=self.cli_timeout_s,
                )
            text = enforce_one_line_50chars(response.stdout)
            provider = response.provider
            elapsed_s = response.elapsed_s
            sources = response.sources or []
            if response.fallback_from:
                provider = f"{provider}:fallback"
            if not response.success:
                if not text and event.panel_name == "fact_checker":
                    text = "근거부족: 응답 생성 실패"
                provider = response.provider

        with self._lock:
            if not self._is_primary_panel(event.panel_name):
                result = self._finish_feed_item(event, text, provider, elapsed_s)
            else:
                current = self.panels[event.panel_name]
                current.text = text or "응답 없음"
                current.importance = max(event.importance, self._importance_from_text(current.text))
                current.provider = provider
                current.elapsed_s = elapsed_s
                current.reason = event.reason
                current.sources = sources
                current.updated_at = datetime.now().isoformat(timespec="seconds")
                current.status = "idle"
                self._apply_panel_visual_spec(
                    current,
                    self._primary_visual_spec(
                        panel_name=event.panel_name,
                        importance=current.importance,
                        text=current.text,
                        status=current.status,
                    ),
                )
                result = PanelOutput(**current.as_dict())

        self._maybe_apply_exaone_layout(event, used_fixture=used_fixture)
        if not self._is_primary_panel(event.panel_name):
            return result
        with self._lock:
            return PanelOutput(**self.panels[event.panel_name].as_dict())

    def state(self) -> dict:
        with self._lock:
            return {
                "running": self.running,
                "mic_running": self.mic_running,
                "ai_provider": self.ai_provider,
                "layout": self.layout.as_dict(),
                "enabled_panels": sorted(self.enabled_panels),
                "panels": {
                    name: panel.as_dict()
                    for name, panel in self.panels.items()
                },
                "feed": [item.as_dict() for item in self.feed[-_MAX_FEED_ITEMS:]],
                "panel_configs": self.panel_configs,
                "transcript": [item.as_dict() for item in self.transcript[-40:]],
                "context": self.context.as_dict(),
            }

    def _render_prompt(self, panel_name: str) -> str:
        with self._lock:
            transcript = self.buffer.as_text()
            context = self.context.as_prompt_block()
        return context + render(panel_name, transcript)

    def _importance_from_text(self, text: str) -> int:
        high_markers = ("확인", "리스크", "위험", "결정", "마감", "비용", "보안", "담당")
        if any(marker in text for marker in high_markers):
            return 3
        return 1

    def _is_primary_panel(self, panel_name: str) -> bool:
        return panel_name in _PRIMARY_PANEL_NAMES or _PANEL_SURFACES.get(panel_name) == "panel"

    def _normalize_ai_provider(self, provider: str) -> str:
        if provider in {"exaone", "friendli"}:
            return "exaone"
        return provider if provider == "cli" else "exaone"

    def _primary_visual_spec(
        self,
        panel_name: str,
        importance: int,
        text: str = "",
        status: str = "idle",
    ) -> dict:
        tone = "neutral"
        emphasis = "none"
        density = "normal"
        urgency = max(1, min(3, importance))
        badges = [_PANEL_TITLES.get(panel_name, panel_name)]

        if panel_name == "summarizer":
            tone = "opportunity" if importance >= 2 else "neutral"
            badges.append("요약")
        elif panel_name == "fact_checker":
            badges.append("검증")
            if text.startswith(("틀림:", "근거부족:")) or "근거부족" in text:
                tone = "danger"
                emphasis = "strong"
                urgency = max(urgency, 3)
            elif text.startswith("맞음:"):
                tone = "neutral"

        if status == "thinking":
            emphasis = "subtle"
        elif importance >= 3 and emphasis == "none":
            emphasis = "pulse"
        elif importance >= 2 and emphasis == "none":
            emphasis = "strong"

        return {
            "runtime": "ggui-compatible",
            "component": _PRIMARY_COMPONENTS.get(panel_name, "primary-panel"),
            "panel": panel_name,
            "importance": max(1, min(3, importance)),
            "urgency": urgency,
            "tone": tone,
            "emphasis": emphasis,
            "density": density,
            "badges": list(dict.fromkeys(badges)),
        }

    def _apply_panel_visual_spec(self, panel: PanelOutput, spec: dict) -> None:
        safe_spec = dict(spec or {})
        tone = safe_spec.get("tone") if safe_spec.get("tone") in _TONES else "neutral"
        emphasis = safe_spec.get("emphasis") if safe_spec.get("emphasis") in _EMPHASIS else "none"
        density = safe_spec.get("density") if safe_spec.get("density") in _DENSITIES else "normal"
        safe_spec["tone"] = tone
        safe_spec["emphasis"] = emphasis
        safe_spec["density"] = density
        safe_spec.setdefault("runtime", "ggui-compatible")
        safe_spec.setdefault("component", _PRIMARY_COMPONENTS.get(panel.panel_name, "primary-panel"))
        safe_spec.setdefault("panel", panel.panel_name)
        panel.tone = tone
        panel.emphasis = emphasis
        panel.density = density
        panel.visual_spec = safe_spec

    def _local_layout_spec(self, mode: str, reason: str, focus_panel: str = "") -> dict:
        mode = mode if mode in _LAYOUT_COLUMNS else "normal"
        panel_visuals: dict[str, dict] = {}
        if focus_panel in self.panels:
            panel_visuals[focus_panel] = {
                "runtime": "ggui-compatible",
                "component": _PRIMARY_COMPONENTS.get(focus_panel, "primary-panel"),
                "panel": focus_panel,
                "tone": "danger" if focus_panel == "fact_checker" or mode == "critical" else "opportunity",
                "emphasis": "pulse" if mode == "critical" else "strong",
                "density": "expanded",
                "importance": 3 if mode == "critical" else 2,
                "urgency": 3 if mode == "critical" else 2,
            }
        elif focus_panel:
            panel_visuals["timeline"] = {
                "runtime": "ggui-compatible",
                "component": "timeline-panel",
                "panel": "timeline",
                "tone": "danger" if mode == "critical" else "action",
                "emphasis": "pulse" if mode == "critical" else "strong",
                "density": "expanded",
                "importance": 3 if mode == "critical" else 2,
                "urgency": 3 if mode == "critical" else 2,
            }
        return {
            "runtime": "ggui-compatible",
            "component": "workspace-layout",
            "layout_mode": mode,
            "columns": _LAYOUT_COLUMNS[mode],
            "panel_visuals": panel_visuals,
            "reason": reason[:90],
            "expires_after_s": 12,
        }

    def _maybe_apply_exaone_layout(self, event: TriggerEvent, used_fixture: bool) -> None:
        with self._lock:
            should_call = (
                self.layout.arbiter_enabled
                and self.ai_provider == "exaone"
                and not self.mock_ai
                and not used_fixture
            )
        if not should_call:
            return

        prompt = self._render_ui_director_prompt(event)
        response = call_exaone_ui_director(
            prompt,
            timeout_s=min(_UI_DIRECTOR_TIMEOUT_S, self.cli_timeout_s),
        )
        if not response.success or not response.stdout:
            return

        try:
            spec = json.loads(response.stdout)
        except json.JSONDecodeError:
            return

        with self._lock:
            if self.layout.arbiter_enabled:
                self._apply_ggui_layout_spec(spec, source="exaone")

    def _render_ui_director_prompt(self, event: TriggerEvent) -> str:
        with self._lock:
            payload = {
                "triggered_event": {
                    "panel": event.panel_name,
                    "reason": event.reason,
                    "importance": event.importance,
                    "utterance": event.utterance[:180],
                },
                "layout": self.layout.as_dict(),
                "panels": {
                    name: {
                        "text": panel.text[:120],
                        "importance": panel.importance,
                        "status": panel.status,
                        "reason": panel.reason,
                        "sources": len(panel.sources),
                    }
                    for name, panel in self.panels.items()
                },
                "timeline": [
                    {
                        "panel": item.panel_name,
                        "text": item.text[:100],
                        "importance": item.importance,
                        "urgency": item.urgency,
                        "tone": item.tone,
                        "status": item.status,
                    }
                    for item in self.feed[-8:]
                ],
                "recent_utterances": [
                    {
                        "speaker": item.speaker,
                        "text": item.text[:160],
                    }
                    for item in self.transcript[-6:]
                ],
            }
        return (
            "아래 회의 UI 상태를 보고 A/B/C 패널의 ggui layout spec을 JSON으로만 반환하세요. "
            "글자 크기 변경은 금지이며, 크기 비율과 강조만 결정하세요.\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    def _apply_ggui_layout_spec(self, spec: dict, source: str) -> None:
        if not isinstance(spec, dict):
            return
        mode = str(spec.get("layout_mode") or spec.get("mode") or "normal")
        if mode not in _LAYOUT_COLUMNS:
            mode = "normal"
        columns = spec.get("columns")
        if not isinstance(columns, list) or len(columns) != 3:
            columns = _LAYOUT_COLUMNS[mode]
        safe_columns = []
        for index, value in enumerate(columns):
            try:
                safe_columns.append(round(max(0.4, min(1.8, float(value))), 2))
            except (TypeError, ValueError):
                safe_columns.append(_LAYOUT_COLUMNS[mode][index])

        panel_visuals = spec.get("panel_visuals") if isinstance(spec.get("panel_visuals"), dict) else {}
        safe_spec = {
            "runtime": "ggui-compatible",
            "component": "workspace-layout",
            "layout_mode": mode,
            "columns": safe_columns,
            "panel_visuals": panel_visuals,
            "reason": str(spec.get("reason") or "")[:90],
            "expires_after_s": self._clamp_int(spec.get("expires_after_s"), 12, 5, 90),
        }
        self.layout.mode = mode
        self.layout.reason = safe_spec["reason"]
        self.layout.ggui_spec = safe_spec
        self.layout.source = source
        self.layout.updated_at = datetime.now().isoformat(timespec="seconds")

        for raw_key, raw_visual in panel_visuals.items():
            panel_name = self._normalize_director_panel_key(raw_key)
            if panel_name not in self.panels or not isinstance(raw_visual, dict):
                continue
            panel = self.panels[panel_name]
            visual = dict(panel.visual_spec or {})
            visual.update(raw_visual)
            visual["importance"] = max(panel.importance, self._clamp_int(visual.get("importance"), 1, 1, 3))
            visual["urgency"] = self._clamp_int(visual.get("urgency"), visual["importance"], 1, 3)
            panel.importance = max(panel.importance, visual["importance"])
            self._apply_panel_visual_spec(panel, visual)

    def _normalize_director_panel_key(self, key: object) -> str:
        value = str(key)
        if value in {"a", "summary"}:
            return "summarizer"
        if value in {"b", "fact"}:
            return "fact_checker"
        return value

    def _clamp_int(self, value: object, default: int, low: int, high: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    def _visual_for_event(
        self,
        panel_name: str,
        importance: int,
        text: str = "",
    ) -> tuple[int, str, str, list[str]]:
        urgency = 1
        variant = "note"
        tone = "neutral"
        badges = [_PANEL_TITLES.get(panel_name, panel_name)]

        if panel_name == "ideator":
            variant = "idea"
            tone = "opportunity"
            badges.append("대안")
        elif panel_name == "devils_advocate":
            variant = "risk"
            tone = "danger"
            urgency = 3
            badges.append("리스크")
        elif panel_name == "parking_lot":
            variant = "question"
            tone = "pending"
            urgency = 2
            badges.append("확인")
        elif panel_name == "action_candidate":
            variant = "task"
            tone = "action"
            urgency = 3 if any(marker in text for marker in ("오늘", "내일", "마감", "즉시")) else 2
            badges.append("액션")
        elif panel_name == "customer_perspective":
            variant = "customer"
            tone = "customer"
            badges.append("고객")

        if importance >= 3:
            urgency = max(urgency, 3)
            badges.append("중요")
        elif importance >= 2:
            urgency = max(urgency, 2)

        return urgency, variant, tone, list(dict.fromkeys(badges))

    def _visual_spec(
        self,
        panel_name: str,
        importance: int,
        urgency: int,
        variant: str,
        tone: str,
        badges: list[str],
    ) -> dict:
        return {
            "runtime": "ggui-compatible",
            "component": "insight-card",
            "panel": panel_name,
            "importance": importance,
            "urgency": urgency,
            "variant": variant,
            "tone": tone,
            "badges": badges,
        }

    def _update_layout(self, panel_name: str, importance: int, reason: str) -> None:
        if not self.layout.arbiter_enabled:
            return
        mode = "normal"
        if importance >= 3 or panel_name == "devils_advocate":
            mode = "critical"
        elif importance >= 2 or panel_name in {"action_candidate", "parking_lot"}:
            mode = "expanded"
        if mode != self.layout.mode or self.layout.source != "local":
            self.layout.mode = mode
            self.layout.reason = reason
            self.layout.ggui_spec = self._local_layout_spec(mode, reason, focus_panel=panel_name)
            self.layout.source = "local"
            self.layout.updated_at = datetime.now().isoformat(timespec="seconds")

    def _fixture_payload_for_panel(
        self,
        fixture_outputs: dict | None,
        panel_name: str,
    ) -> dict:
        payload = {
            "text": None,
            "delay_s": None,
            "detail_title": "",
            "detail_body": "",
            "detail_points": [],
            "detail_action": "",
        }
        if not fixture_outputs:
            return payload
        raw = fixture_outputs.get(panel_name)
        if raw is None:
            return payload
        if isinstance(raw, str):
            payload["text"] = raw
            return payload
        if isinstance(raw, dict):
            text = str(raw.get("text", "")).strip()
            delay = raw.get("delay_s")
            try:
                delay_s = float(delay) if delay is not None else None
            except (TypeError, ValueError):
                delay_s = None
            detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
            payload.update({
                "text": text or None,
                "delay_s": delay_s,
                "detail_title": self._clean_detail_text(
                    raw.get("detail_title") or detail.get("title"),
                    limit=40,
                ),
                "detail_body": self._clean_detail_text(
                    raw.get("detail_body") or detail.get("body"),
                    limit=180,
                ),
                "detail_points": self._clean_detail_points(
                    raw.get("detail_points") or detail.get("points"),
                ),
                "detail_action": self._clean_detail_text(
                    raw.get("detail_action") or detail.get("action"),
                    limit=90,
                ),
            })
            return payload
        payload["text"] = str(raw)
        return payload

    def _clean_detail_text(self, value: object, limit: int) -> str:
        text = str(value or "").strip()
        return text[:limit]

    def _clean_detail_points(self, value: object) -> list[str]:
        if isinstance(value, str):
            raw_points = [value]
        elif isinstance(value, list):
            raw_points = value
        else:
            raw_points = []
        points = [
            self._clean_detail_text(point, limit=90)
            for point in raw_points
            if self._clean_detail_text(point, limit=90)
        ]
        return points[:5]

    def _create_feed_item(self, event: TriggerEvent) -> InsightFeedItem:
        self._feed_seq += 1
        now = datetime.now().isoformat(timespec="seconds")
        urgency, variant, tone, badges = self._visual_for_event(
            event.panel_name,
            event.importance,
            event.utterance,
        )
        self._update_layout(event.panel_name, event.importance, event.reason)
        return InsightFeedItem(
            id=f"feed-{self._feed_seq}",
            panel_name=event.panel_name,
            label=_PANEL_TITLES.get(event.panel_name, event.panel_name),
            text=self._thinking_text(event),
            importance=event.importance,
            urgency=urgency,
            card_variant=variant,
            tone=tone,
            badges=badges,
            visual_spec=self._visual_spec(
                event.panel_name,
                event.importance,
                urgency,
                variant,
                tone,
                badges,
            ),
            reason=event.reason,
            provider="",
            elapsed_s=0.0,
            updated_at=now,
            status="thinking",
            detail_title=_PANEL_TITLES.get(event.panel_name, event.panel_name),
        )

    def _finish_feed_item(
        self,
        event: TriggerEvent,
        text: str,
        provider: str,
        elapsed_s: float,
    ) -> InsightFeedItem:
        item = next(
            (feed_item for feed_item in self.feed if feed_item.id == event.output_id),
            None,
        )
        if item is None:
            item = self._create_feed_item(event)
            event.output_id = item.id
            self.feed.append(item)
            self.feed = self.feed[-_MAX_FEED_ITEMS:]

        item.text = text or "응답 없음"
        item.importance = max(event.importance, self._importance_from_text(item.text))
        item.urgency, item.card_variant, item.tone, item.badges = self._visual_for_event(
            event.panel_name,
            item.importance,
            item.text,
        )
        item.visual_spec = self._visual_spec(
            event.panel_name,
            item.importance,
            item.urgency,
            item.card_variant,
            item.tone,
            item.badges,
        )
        self._update_layout(event.panel_name, item.importance, event.reason)
        item.provider = provider
        item.elapsed_s = elapsed_s
        item.reason = event.reason
        detail = self._detail_for_event(event, item.text)
        item.detail_title = event.fixture_detail_title or detail["title"]
        item.detail_body = event.fixture_detail_body or detail["body"]
        item.detail_points = event.fixture_detail_points or detail["points"]
        item.detail_action = event.fixture_detail_action or detail["action"]
        item.updated_at = datetime.now().isoformat(timespec="seconds")
        item.status = "idle"
        return InsightFeedItem(**item.as_dict())

    def _detail_for_event(self, event: TriggerEvent, text: str) -> dict:
        focus = (event.focus_keyword or "최근 논점").strip()
        if event.panel_name == "devils_advocate":
            return {
                "title": "반박 포인트",
                "body": f"{text} 관점에서 결정 전에 확인할 리스크입니다.",
                "points": [
                    "합의가 너무 빠른지 확인",
                    "비용·일정·고객 영향의 반대 사례 검토",
                    "결정 조건과 중단 기준 명시",
                ],
                "action": "결정 전에 리스크 기준을 한 줄로 추가",
            }
        if event.panel_name == "ideator":
            return {
                "title": "아이디어 확장",
                "body": f"{focus}을 기준으로 화면이나 논의를 확장할 수 있습니다.",
                "points": [
                    "사용자가 비교할 수 있는 선택지로 분리",
                    "현재 안과 대안의 조건을 나란히 제시",
                    "검증할 데이터 기준을 먼저 정리",
                ],
                "action": "다음 화면 초안에 대안 카드 추가",
            }
        if event.panel_name == "parking_lot":
            return {
                "title": "남은 확인 항목",
                "body": f"{text} 항목은 결론 전에 별도 확인이 필요합니다.",
                "points": [
                    "담당자와 확인 기한 지정",
                    "필요한 출처나 데이터 범위 확정",
                    "다음 회의에서 결론 여부만 점검",
                ],
                "action": "확인 담당자와 마감일 기록",
            }
        if event.panel_name == "action_candidate":
            return {
                "title": "실행 항목",
                "body": f"{text} 작업을 바로 실행 가능한 단위로 정리합니다.",
                "points": [
                    "담당자 지정",
                    "완료 기준과 마감일 명시",
                    "공유 위치와 후속 확인 방식 결정",
                ],
                "action": "액션 아이템 목록에 추가",
            }
        if event.panel_name == "customer_perspective":
            return {
                "title": "고객 관점",
                "body": f"{text} 관점에서 사용자가 실제로 묻는 질문을 좁힙니다.",
                "points": [
                    "사용자가 바로 이해할 표현으로 바꾸기",
                    "기능 설명보다 체감 효과 우선",
                    "불안하거나 헷갈릴 지점 선제 대응",
                ],
                "action": "화면 문구를 고객 질문형으로 수정",
            }
        return {
            "title": _PANEL_TITLES.get(event.panel_name, event.panel_name),
            "body": text,
            "points": [event.reason] if event.reason else [],
            "action": "",
        }

    def _thinking_text(self, event: TriggerEvent) -> str:
        keyword = (event.focus_keyword or "최근 발화").strip()
        return enforce_one_line_50chars(f"{keyword} 분석중...")

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
        if event.panel_name == "parking_lot":
            return enforce_one_line_50chars("미해결: 확인 담당자와 기준 필요")
        if event.panel_name == "action_candidate":
            return enforce_one_line_50chars("TODO: 담당자와 마감일을 명확히 지정")
        if event.panel_name == "customer_perspective":
            return enforce_one_line_50chars("고객은 이 변경을 언제 체감하나요?")
        return enforce_one_line_50chars(text)
