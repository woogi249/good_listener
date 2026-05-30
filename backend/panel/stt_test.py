"""마이크 녹음 + Whisper 한국어 전사 테스트.

사용법:
  python -m panel.stt_test              # 15초 녹음 + 전사
  python -m panel.stt_test --duration 30
  python -m panel.stt_test --model medium
  python -m panel.stt_test --file path/to/existing.wav   # 기존 WAV 전사만
  python -m panel.stt_test --initial-prompt "..."        # 도메인 어휘 힌트
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.io import wavfile

from .stt import transcribe as _transcribe
from .vocabulary import hotwords_for_profile, load_domain_prompt

SAMPLE_RATE = 16000
CHANNELS = 1
DEFAULT_OUT = Path(__file__).resolve().parent.parent.parent / "samples" / "stt_test.wav"


def record(duration_s: int, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    print(f"▶ {duration_s}초 녹음 시작 — 지금부터 말씀하세요 (한국어)!", flush=True)
    audio = sd.rec(int(duration_s * sample_rate), samplerate=sample_rate, channels=CHANNELS, dtype="int16")
    for sec in range(duration_s, 0, -1):
        print(f"  남은 시간: {sec}초", flush=True)
        sd.sleep(1000)
    sd.wait()
    print("■ 녹음 완료", flush=True)
    return audio


def main() -> int:
    parser = argparse.ArgumentParser(description="Whisper 한국어 STT 테스트")
    parser.add_argument("--duration", type=int, default=15, help="녹음 길이(초). 기본 15")
    parser.add_argument("--model", default="small", choices=["tiny", "base", "small", "medium", "large-v3"], help="Whisper 모델 크기")
    parser.add_argument("--file", type=Path, help="기존 WAV 파일만 전사")
    parser.add_argument("--initial-prompt", type=str, default=None, help="Whisper initial_prompt (도메인 어휘 힌트)")
    parser.add_argument("--domain-profile", choices=["ai", "none"], default="none", help="도메인 용어집 프로필")
    args = parser.parse_args()

    if args.file:
        wav_path = args.file
        if not wav_path.exists():
            print(f"파일 없음: {wav_path}", file=sys.stderr)
            return 1
    else:
        try:
            audio = record(args.duration)
        except sd.PortAudioError as e:
            print(f"❌ 녹음 실패: {e}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\n중단됨.", file=sys.stderr)
            return 130

        DEFAULT_OUT.parent.mkdir(parents=True, exist_ok=True)
        wavfile.write(DEFAULT_OUT, SAMPLE_RATE, audio)
        wav_path = DEFAULT_OUT
        print(f"  저장: {wav_path} ({wav_path.stat().st_size / 1024:.1f} KB)\n", flush=True)

    profile = None if args.domain_profile == "none" else args.domain_profile
    initial_prompt = args.initial_prompt or load_domain_prompt(profile)
    result = _transcribe(
        wav_path=wav_path,
        model_size=args.model,
        initial_prompt=initial_prompt,
        hotwords=hotwords_for_profile(profile),
        correction_profile=profile,
        verbose=True,
    )

    full_text = " ".join(s.text for s in result.segments)
    print(f"\n=== 최종 전사 결과 ===")
    print(full_text)
    print(f"\n처리 속도: {result.elapsed_s:.2f}s (녹음 길이 대비 배속 계산은 --file로 길이 재사용)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
