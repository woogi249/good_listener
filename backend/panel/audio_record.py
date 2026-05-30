"""마이크 녹음 테스트 스크립트.

5초 녹음하여 WAV 저장 + 품질 지표(피크·RMS dBFS·무음 비율) 출력.
실행: python -m panel.audio_record
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.io import wavfile


SAMPLE_RATE = 16000    # Whisper 권장 입력 샘플링 레이트
CHANNELS = 1           # 모노
DURATION_S = 5
DEFAULT_OUT = Path(__file__).resolve().parent.parent.parent / "samples" / "recording_test.wav"


def list_input_devices() -> None:
    print("=== 사용 가능한 입력 장치 ===")
    default_in = sd.default.device[0] if sd.default.device[0] is not None else -1
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] <= 0:
            continue
        marker = " ← 기본" if i == default_in else ""
        print(f"  [{i}] {dev['name']}  (채널 {dev['max_input_channels']}, {int(dev['default_samplerate'])}Hz){marker}")
    print()


def record(duration_s: int = DURATION_S, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    print(f"▶ {duration_s}초 녹음 시작 — 지금 마이크에 말씀하세요!", flush=True)
    # 카운트다운
    for sec in range(duration_s, 0, -1):
        audio = sd.rec(int(1 * sample_rate), samplerate=sample_rate, channels=CHANNELS, dtype="int16")
        sd.wait()
        print(f"  [{sec} → {sec-1}] 녹음 중...", flush=True)
        if sec == duration_s:
            all_audio = audio
        else:
            all_audio = np.vstack([all_audio, audio])
    print("■ 녹음 완료", flush=True)
    return all_audio


def analyze(audio: np.ndarray, sample_rate: int) -> dict:
    flat = audio.flatten().astype(np.int32)
    peak = int(np.max(np.abs(flat)))
    rms = float(np.sqrt(np.mean(flat.astype(np.float64) ** 2)))
    # int16 full scale = 32767
    peak_dbfs = 20 * np.log10(peak / 32767) if peak > 0 else -100.0
    rms_dbfs = 20 * np.log10(rms / 32767) if rms > 0 else -100.0
    silence_threshold = 200  # int16 amplitude
    silence_ratio = float(np.sum(np.abs(flat) < silence_threshold)) / len(flat)
    return {
        "duration_s": round(len(flat) / sample_rate, 3),
        "samples": int(len(flat)),
        "sample_rate": int(sample_rate),
        "peak_int16": peak,
        "peak_dBFS": round(peak_dbfs, 2),
        "rms_dBFS": round(rms_dbfs, 2),
        "silence_ratio": round(silence_ratio, 3),
    }


def interpret(stats: dict) -> str:
    peak = stats["peak_dBFS"]
    silence = stats["silence_ratio"]
    if peak < -60:
        return "❌ 녹음 신호 거의 없음 (마이크 미연결/권한 거부/음소거 가능성)"
    if silence > 0.95:
        return "⚠️ 대부분 무음 (5초간 말하지 않았거나 마이크 감도 낮음)"
    if peak > -3:
        return "⚠️ 클리핑 가능성 (마이크 입력 레벨 과다)"
    if -30 <= peak <= -6 and silence < 0.7:
        return "✅ 양호한 녹음 (정상 말하기 수준)"
    return "🟡 녹음됨 (신호 약하거나 간헐적)"


def main() -> int:
    try:
        list_input_devices()
    except Exception as e:
        print(f"장치 목록 조회 실패: {e}", file=sys.stderr)
        return 1

    try:
        audio = record()
    except sd.PortAudioError as e:
        print(f"❌ 녹음 실패 — {e}", file=sys.stderr)
        print("원인 추정: 마이크 권한 거부 또는 장치 사용 중.", file=sys.stderr)
        print("Windows 설정 → 개인정보 보호 → 마이크 → Python/Terminal 허용 여부 확인.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n중단됨.", file=sys.stderr)
        return 130

    DEFAULT_OUT.parent.mkdir(parents=True, exist_ok=True)
    wavfile.write(DEFAULT_OUT, SAMPLE_RATE, audio)

    stats = analyze(audio, SAMPLE_RATE)
    print(f"\n=== 녹음 결과 ===")
    print(f"  저장 경로: {DEFAULT_OUT}")
    print(f"  파일 크기: {DEFAULT_OUT.stat().st_size / 1024:.1f} KB")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print(f"\n판정: {interpret(stats)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
