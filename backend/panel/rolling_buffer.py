"""롤링 발화 버퍼.

DR-04 데이터 경계 준수: 이 버퍼만 CLI 전송 허용. 전체 회의 트랜스크립트 전송 금지.
기본 크기 10발화 (DR-02 프롬프트 토큰 예산과 연동).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Utterance:
    timestamp: datetime
    speaker: str  # MVP는 "unknown" 고정. 화자 분리는 Phase 2 (DR-03 Fallback 조건 C).
    text: str


class RollingBuffer:
    def __init__(self, maxlen: int = 10):
        if maxlen <= 0:
            raise ValueError("maxlen must be positive")
        self._buf: deque[Utterance] = deque(maxlen=maxlen)

    def add(self, utterance: Utterance) -> None:
        self._buf.append(utterance)

    def snapshot(self) -> list[Utterance]:
        return list(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

    def is_full(self) -> bool:
        return len(self._buf) == self._buf.maxlen

    def as_text(self) -> str:
        """프롬프트 주입용 직렬화. 발화당 1줄, 앞에 `- ` 붙임."""
        return "\n".join(f"- {u.text}" for u in self._buf)
