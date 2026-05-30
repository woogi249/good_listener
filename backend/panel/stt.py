"""Whisper 기반 한국어 STT 전사 모듈.

panel.stt_test (대화형 테스트)와 panel.runner (파이프라인)가 공유하는
순수 전사 함수. 녹음·파이프라인 결합은 호출자 책임.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .vocabulary import correct_domain_terms


@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float
    text: str


@dataclass(frozen=True)
class TranscriptionResult:
    segments: list[Segment]
    language: str
    language_probability: float
    elapsed_s: float           # 전사 시간만 (모델 로드 제외)
    model_load_s: float
    audio_duration_s: float
    model_size: str


def _collect_segments(raw_segments: Any, verbose: bool = False) -> list[Segment]:
    segments: list[Segment] = []
    for seg in raw_segments:
        text = seg.text.strip()
        if not text:
            continue
        s = Segment(start_s=seg.start, end_s=seg.end, text=text)
        segments.append(s)
        if verbose:
            print(f"  [{s.start_s:5.2f}s → {s.end_s:5.2f}s] {s.text}", flush=True)
    return segments


def _correct_segments(
    segments: list[Segment],
    correction_profile: str | None,
) -> list[Segment]:
    if correction_profile in (None, "", "none"):
        return segments
    return [
        Segment(
            start_s=segment.start_s,
            end_s=segment.end_s,
            text=correct_domain_terms(segment.text, correction_profile),
        )
        for segment in segments
    ]


class WhisperTranscriber:
    """실시간 청크 처리용 Whisper 모델 캐시."""

    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
    ):
        from faster_whisper import WhisperModel

        t0 = time.perf_counter()
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.model_load_s = time.perf_counter() - t0
        self.model_size = model_size
        self.beam_size = beam_size

    def transcribe_audio(
        self,
        audio: Any,
        sample_rate: int = 16000,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
        correction_profile: str | None = None,
    ) -> TranscriptionResult:
        """numpy waveform 청크를 전사한다."""
        t1 = time.perf_counter()
        raw_segments, info = self.model.transcribe(
            audio,
            language="ko",
            beam_size=self.beam_size,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            vad_filter=True,
        )
        segments = _correct_segments(_collect_segments(raw_segments), correction_profile)
        elapsed = time.perf_counter() - t1
        try:
            audio_duration_s = len(audio) / sample_rate
        except TypeError:
            audio_duration_s = info.duration
        return TranscriptionResult(
            segments=segments,
            language=info.language,
            language_probability=info.language_probability,
            elapsed_s=elapsed,
            model_load_s=self.model_load_s,
            audio_duration_s=audio_duration_s,
            model_size=self.model_size,
        )


def transcribe(
    wav_path: Path,
    model_size: str = "small",
    initial_prompt: str | None = None,
    hotwords: str | None = None,
    correction_profile: str | None = None,
    device: str = "cpu",
    compute_type: str = "int8",
    beam_size: int = 5,
    verbose: bool = False,
) -> TranscriptionResult:
    """WAV를 한국어로 전사.

    - 빈 세그먼트(text.strip() == "")는 결과에서 제외.
    - elapsed_s는 전사 시간만. model_load_s는 별도 필드.
    - verbose=True면 진행 상황을 stdout으로 출력 (stt_test 기존 포맷).
    """
    from faster_whisper import WhisperModel

    if verbose:
        print(f"▶ Whisper 모델 로드: {model_size} ({device}, {compute_type})...", flush=True)
    t0 = time.perf_counter()
    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    model_load_s = time.perf_counter() - t0
    if verbose:
        print(f"  모델 로드: {model_load_s:.2f}s", flush=True)
        if initial_prompt:
            print(f"  initial_prompt: {initial_prompt!r}", flush=True)
        print(f"▶ 전사 시작: {wav_path}", flush=True)

    t1 = time.perf_counter()
    raw_segments, info = model.transcribe(
        str(wav_path),
        language="ko",
        beam_size=beam_size,
        initial_prompt=initial_prompt,
        hotwords=hotwords,
    )
    segments = _correct_segments(_collect_segments(raw_segments), correction_profile)
    if verbose:
        for s in segments:
            print(f"  [{s.start_s:5.2f}s → {s.end_s:5.2f}s] {s.text}", flush=True)
    elapsed = time.perf_counter() - t1
    if verbose:
        print(
            f"■ 전사 완료: {elapsed:.2f}s (lang={info.language}, "
            f"prob={info.language_probability:.2f}, {len(segments)} segments)",
            flush=True,
        )

    return TranscriptionResult(
        segments=segments,
        language=info.language,
        language_probability=info.language_probability,
        elapsed_s=elapsed,
        model_load_s=model_load_s,
        audio_duration_s=info.duration,
        model_size=model_size,
    )
