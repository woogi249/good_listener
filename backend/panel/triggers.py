"""트리거 평가자."""
from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field

from .config import (
    ACTION_CANDIDATE,
    CUSTOMER_PERSPECTIVE,
    DEVILS_ADVOCATE,
    FACT_CHECKER,
    IDEATOR,
    PARKING_LOT,
    REALTIME_PANELS,
    SUMMARIZER,
    TriggerConfig,
)
from .rolling_buffer import RollingBuffer


@dataclass
class TriggerState:
    config: TriggerConfig
    last_fired_at: float = field(default=0.0)  # time.monotonic()
    counter: int = 0

    def evaluate_on_new_utterance(self) -> bool:
        """새 발화가 들어왔을 때 트리거 발동 여부를 평가한다.

        트리거 조건:
        1. 활성화된 패널
        2. 누적 발화 수 >= threshold
        3. 직전 발동 이후 cooldown_s 경과
        """
        if not self.config.enabled:
            return False
        self.counter += 1
        if self.counter < self.config.utterance_threshold:
            return False
        now = time.monotonic()
        if (now - self.last_fired_at) < self.config.cooldown_s and self.last_fired_at > 0:
            return False
        return True

    def mark_fired(self) -> None:
        """트리거가 발동되고 CLI 호출을 시작했을 때 호출한다."""
        self.last_fired_at = time.monotonic()
        self.counter = 0


@dataclass
class TriggerEvent:
    panel_name: str
    reason: str
    importance: int
    priority: str
    utterance: str
    focus_keyword: str = "최근 발화"
    output_id: str | None = None
    fixture_text: str | None = None
    fixture_delay_s: float | None = None
    fixture_detail_title: str = ""
    fixture_detail_body: str = ""
    fixture_detail_points: list[str] = field(default_factory=list)
    fixture_detail_action: str = ""


_NUMBER_PATTERN = re.compile(
    r"(\d[\d,.]*\s*(%|퍼센트|원|만원|억원|달러|명|건|초|분|시간|일|월|년|GB|MB|ms|s)?)"
)
_FACT_KEYWORDS = (
    "매출",
    "목표",
    "예산",
    "비용",
    "계약",
    "일정",
    "마감",
    "성능",
    "지연",
    "p50",
    "p95",
    "RTF",
    "WER",
    "CAC",
    "CLI",
    "API",
    "STT",
    "GPU",
    "CPU",
)
_AGREEMENT_KEYWORDS = (
    "결정",
    "확정",
    "합의",
    "채택",
    "진행하",
    "가죠",
    "하자",
    "좋습니다",
    "오케이",
    "OK",
    "그럼",
)
_STUCK_KEYWORDS = (
    "문제",
    "고민",
    "막히",
    "어렵",
    "대안",
    "아이디어",
    "방법",
    "어떻게",
    "어떨",
    "까요",
    "논의",
)
_OPEN_QUESTION_KEYWORDS = (
    "확인 필요",
    "확인해야",
    "나중에",
    "추후",
    "모르겠",
    "불확실",
    "질문",
    "누가 확인",
    "검토 필요",
    "?",
)
_ACTION_KEYWORDS = (
    "담당",
    "까지",
    "요청",
    "정리",
    "공유",
    "작성",
    "준비",
    "올리",
    "확인하겠습니다",
    "확인해볼게요",
    "하겠습니다",
    "다음 주",
    "오늘 중",
    "내일",
)
_CUSTOMER_KEYWORDS = (
    "고객",
    "사용자",
    "유저",
    "불편",
    "니즈",
    "가치",
    "도입",
    "전환",
    "가입",
    "이탈",
    "피드백",
    "사용성",
)
_URGENT_KEYWORDS = (
    "마감",
    "결정",
    "확정",
    "리스크",
    "위험",
    "장애",
    "실패",
    "비용",
    "예산",
    "보안",
    "개인정보",
    "고객",
)
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9가-힣]{2,}")
_MODEL_PATTERN = re.compile(
    r"\b(Claude\s+Opus\s+\d+(?:\.\d+)?|Opus\s+\d+(?:\.\d+)?|GPT-\d+(?:\.\d+)?|Codex|Claude|ChatGPT|STT|PoC|MVP|CLI|API|CAC|WER|RTF)\b",
    re.IGNORECASE,
)
_NUMBER_CONTEXT_PATTERN = re.compile(
    r"\d[\d,.]*\s*(?:%|퍼센트|원|만원|억원|달러|명|건|초|분|시간|일|월|년|GB|MB|ms|s)\s*[A-Za-z0-9가-힣]{0,8}"
)
_FOCUS_PHRASES = (
    "Claude Opus 4.8",
    "Opus 4.8",
    "제조 PoC",
    "30분 회의",
    "고객 인터뷰",
    "현장 용어",
    "평가 지표",
    "정책 문서",
    "반복 코드",
    "신규 산업",
    "개인정보",
    "벤치마크",
    "제조 현장",
    "회의록",
    "법무",
    "의료",
    "Codex",
    "Opus",
)
_FOCUS_STOPWORDS = {
    "오늘은",
    "최근",
    "제가",
    "우리",
    "그럼",
    "좋습니다",
    "다만",
    "반면",
    "회의",
    "모델",
    "사용자",
    "고객",
    "정도",
    "부분",
    "기준",
}


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _importance(text: str, default: int) -> int:
    score = default
    if _NUMBER_PATTERN.search(text):
        score += 1
    if _contains_any(text, _URGENT_KEYWORDS):
        score += 1
    return min(3, max(1, score))


def _keywords_from_buffer(buffer: RollingBuffer, limit: int = 8) -> Counter[str]:
    utterances = buffer.snapshot()[-limit:]
    tokens: list[str] = []
    for utterance in utterances:
        tokens.extend(_TOKEN_PATTERN.findall(utterance.text))
    stopwords = {"그리고", "그래서", "이번", "오늘", "우리", "그럼", "이제", "있는", "없는"}
    return Counter(token for token in tokens if token not in stopwords)


def _focus_keyword(text: str) -> str:
    hits: list[str] = []

    for phrase in _FOCUS_PHRASES:
        if phrase in text:
            hits.append(phrase)

    for match in _MODEL_PATTERN.finditer(text):
        hits.append(match.group(1).strip())

    for match in _NUMBER_CONTEXT_PATTERN.finditer(text):
        hits.append(match.group(0).strip())

    if not hits:
        for token in _TOKEN_PATTERN.findall(text):
            if token in _FOCUS_STOPWORDS:
                continue
            if len(token) < 2:
                continue
            hits.append(token)
            if len(hits) >= 2:
                break

    deduped: list[str] = []
    for hit in hits:
        clean = re.sub(r"\s+", " ", hit).strip(" ,.;:!?")
        if not clean:
            continue
        clean_lower = clean.lower()
        existing_lowers = [item.lower() for item in deduped]
        if any(clean_lower == existing for existing in existing_lowers):
            continue
        if any(clean_lower in existing for existing in existing_lowers):
            continue
        deduped = [
            item
            for item in deduped
            if item.lower() not in clean_lower
        ]
        deduped.append(clean)

    if not deduped:
        return "최근 발화"

    if len(deduped) >= 2:
        combined = "·".join(deduped[:2])
        if len(combined) <= 24:
            return combined

    return deduped[0][:24]


class RealtimeTriggerEngine:
    """4패널 실시간 트리거.

    룰 기반으로 호출 빈도를 제한한다. LLM은 실제 판단 문장 생성에만 사용한다.
    """

    def __init__(self, configs: tuple[TriggerConfig, ...] = REALTIME_PANELS):
        self.states = {config.panel_name: TriggerState(config) for config in configs}

    def evaluate(
        self,
        text: str,
        buffer: RollingBuffer,
        enabled_panels: set[str] | None = None,
    ) -> list[TriggerEvent]:
        events: list[TriggerEvent] = []
        if enabled_panels is None:
            enabled_panels = set(self.states)

        summary_state = self.states[SUMMARIZER.panel_name]
        if SUMMARIZER.panel_name in enabled_panels and summary_state.evaluate_on_new_utterance():
            events.append(self._event(SUMMARIZER, text, "최근 10발화 요약", _importance(text, 1)))
            summary_state.mark_fired()

        if FACT_CHECKER.panel_name in enabled_panels and self._should_fact_check(text):
            event = self._cooldown_event(FACT_CHECKER, text, "수치/고유명사/검증 주장 감지", _importance(text, 2))
            if event:
                events.append(event)

        if DEVILS_ADVOCATE.panel_name in enabled_panels and _contains_any(text, _AGREEMENT_KEYWORDS):
            event = self._cooldown_event(DEVILS_ADVOCATE, text, "합의/결정 신호 감지", _importance(text, 2))
            if event:
                events.append(event)

        if IDEATOR.panel_name in enabled_panels and self._should_ideate(text, buffer):
            event = self._cooldown_event(IDEATOR, text, "교착/반복 논점 감지", _importance(text, 1))
            if event:
                events.append(event)

        if PARKING_LOT.panel_name in enabled_panels and self._should_capture_open_question(text):
            event = self._cooldown_event(PARKING_LOT, text, "미해결 질문/확인 항목 감지", _importance(text, 2))
            if event:
                events.append(event)

        if ACTION_CANDIDATE.panel_name in enabled_panels and self._should_capture_action(text):
            event = self._cooldown_event(ACTION_CANDIDATE, text, "담당/기한/요청 신호 감지", _importance(text, 2))
            if event:
                events.append(event)

        if CUSTOMER_PERSPECTIVE.panel_name in enabled_panels and self._should_add_customer_perspective(text):
            event = self._cooldown_event(CUSTOMER_PERSPECTIVE, text, "고객/사용자 관점 신호 감지", _importance(text, 1))
            if event:
                events.append(event)

        events.sort(key=lambda item: {"HIGH": 0, "MED": 1, "LOW": 2}[item.priority])
        return events

    def _should_fact_check(self, text: str) -> bool:
        has_number = _NUMBER_PATTERN.search(text) is not None
        has_claim_keyword = _contains_any(text, _FACT_KEYWORDS)
        has_upper_token = re.search(r"\b[A-Z][A-Z0-9-]{1,}\b", text) is not None
        return has_number or has_claim_keyword or has_upper_token

    def _should_ideate(self, text: str, buffer: RollingBuffer) -> bool:
        if _contains_any(text, _STUCK_KEYWORDS):
            return True
        if len(buffer) < 6:
            return False
        common = _keywords_from_buffer(buffer).most_common(1)
        return bool(common and common[0][1] >= 3)

    def _should_capture_open_question(self, text: str) -> bool:
        return _contains_any(text, _OPEN_QUESTION_KEYWORDS)

    def _should_capture_action(self, text: str) -> bool:
        return _contains_any(text, _ACTION_KEYWORDS)

    def _should_add_customer_perspective(self, text: str) -> bool:
        has_customer_keyword = _contains_any(text, _CUSTOMER_KEYWORDS)
        has_decision_keyword = _contains_any(text, _AGREEMENT_KEYWORDS)
        has_product_keyword = _contains_any(text, ("기능", "서비스", "화면", "프로덕트", "경험"))
        return has_customer_keyword or (has_decision_keyword and has_product_keyword)

    def _cooldown_event(
        self,
        config: TriggerConfig,
        text: str,
        reason: str,
        importance: int,
    ) -> TriggerEvent | None:
        if not config.enabled:
            return None
        state = self.states[config.panel_name]
        now = time.monotonic()
        if state.last_fired_at and (now - state.last_fired_at) < config.cooldown_s:
            return None
        state.mark_fired()
        return self._event(config, text, reason, importance)

    def _event(
        self,
        config: TriggerConfig,
        text: str,
        reason: str,
        importance: int,
    ) -> TriggerEvent:
        return TriggerEvent(
            panel_name=config.panel_name,
            reason=reason,
            importance=importance,
            priority=config.priority,
            utterance=text,
            focus_keyword=_focus_keyword(text),
        )
