"""MVP 테스트 러너.

사용법:
  # 1) 텍스트 파일 입력 (한 줄 = 1 발화)
  python -m panel.runner ../samples/mock_transcript.txt

  # 2) 대화형 stdin
  python -m panel.runner --interactive

  # 3) WAV 배치 (Whisper 전사 → 롤링버퍼 → Summarizer)
  python -m panel.runner --wav ../samples/stt_test.wav --model small \
      --initial-prompt-file ../prompts/domain-crypto.txt

각 라인/세그먼트 = 1 발화. 버퍼에 누적하고 Summarizer 트리거가 발동하면
`claude` CLI를 호출하여 1줄 요약을 출력한다.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

from .cli_dispatcher import (
    ClaudeResponse,
    call_claude_with_codex_fallback,
    enforce_one_line_50chars,
)
from .config import DEFAULT_BUFFER_SIZE, DEFAULT_CLI_TIMEOUT_S, SUMMARIZER
from .prompts import render
from .rolling_buffer import RollingBuffer, Utterance
from .triggers import TriggerState
from .vocabulary import hotwords_for_profile, load_domain_prompt

_LATENCY_CSV = (
    Path(__file__).resolve().parent.parent.parent / "samples" / "cli_latency.csv"
)
_LATENCY_HEADER = [
    "timestamp",
    "model",
    "wav_duration_s",
    "segment_count",
    "stt_elapsed_s",
    "cli_elapsed_s",
    "cli_success",
]


def _append_latency_row(row: dict) -> None:
    _LATENCY_CSV.parent.mkdir(parents=True, exist_ok=True)
    new_file = not _LATENCY_CSV.exists()
    with _LATENCY_CSV.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_LATENCY_HEADER)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def process_utterance(
    text: str,
    buffer: RollingBuffer,
    summarizer_state: TriggerState,
    total_count: list[int],
) -> ClaudeResponse | None:
    text = text.strip()
    if not text:
        return None
    total_count[0] += 1
    buffer.add(
        Utterance(
            timestamp=datetime.now(),
            speaker="unknown",
            text=text,
        )
    )
    print(f"[#{total_count[0]:02d} | buf={len(buffer):02d}] {text}")

    if summarizer_state.evaluate_on_new_utterance():
        print("  → Summarizer 트리거 발동, claude CLI 호출 중... (실패 시 codex fallback)")
        prompt = render("summarizer", buffer.as_text())
        response = call_claude_with_codex_fallback(
            prompt,
            timeout_s=DEFAULT_CLI_TIMEOUT_S,
        )
        summarizer_state.mark_fired()
        if response.success:
            summary = enforce_one_line_50chars(response.stdout)
            provider = response.provider
            if response.fallback_from:
                provider = f"{provider}, fallback"
            print(f"  ✓ 요약 [{provider}] ({response.elapsed_s:.2f}s): {summary}")
        else:
            print(f"  ✗ 실패 ({response.elapsed_s:.2f}s): {response.error}")
            if response.stderr:
                print(f"    stderr: {response.stderr[:200]}")
        return response
    return None


def run_from_file(path: Path) -> None:
    buffer = RollingBuffer(maxlen=DEFAULT_BUFFER_SIZE)
    summarizer = TriggerState(config=SUMMARIZER)
    total_count = [0]
    for line in path.read_text(encoding="utf-8").splitlines():
        process_utterance(line, buffer, summarizer, total_count)


def run_interactive() -> None:
    buffer = RollingBuffer(maxlen=DEFAULT_BUFFER_SIZE)
    summarizer = TriggerState(config=SUMMARIZER)
    total_count = [0]
    print("대화형 모드. 발화를 한 줄씩 입력. Ctrl+D(Linux/Mac) 또는 Ctrl+Z Enter(Windows)로 종료.")
    try:
        for line in sys.stdin:
            process_utterance(line, buffer, summarizer, total_count)
    except KeyboardInterrupt:
        print("\n중단됨.")


def run_from_wav(
    wav_path: Path,
    model: str,
    initial_prompt_file: Path | None,
    domain_profile: str | None = None,
) -> int:
    from .stt import transcribe

    if not wav_path.exists():
        print(f"WAV 파일 없음: {wav_path}", file=sys.stderr)
        return 1

    if initial_prompt_file is not None:
        if not initial_prompt_file.exists():
            print(
                f"initial-prompt-file not found: {initial_prompt_file}",
                file=sys.stderr,
            )
            return 1
        try:
            prompt = load_domain_prompt(domain_profile, initial_prompt_file)
        except UnicodeDecodeError as e:
            print(
                f"initial-prompt-file is not UTF-8: {initial_prompt_file} ({e})",
                file=sys.stderr,
            )
            return 1
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    else:
        try:
            prompt = load_domain_prompt(domain_profile)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 1
    try:
        hotwords = hotwords_for_profile(domain_profile)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    try:
        result = transcribe(
            wav_path=wav_path,
            model_size=model,
            initial_prompt=prompt,
            hotwords=hotwords,
            correction_profile=domain_profile,
            verbose=True,
        )
    except Exception as e:
        print(f"전사 실패: {e}", file=sys.stderr)
        return 2

    print()
    buffer = RollingBuffer(maxlen=DEFAULT_BUFFER_SIZE)
    summarizer = TriggerState(config=SUMMARIZER)
    total_count = [0]

    for seg in result.segments:
        response = process_utterance(seg.text, buffer, summarizer, total_count)
        if response is not None:
            _append_latency_row({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "model": model,
                "wav_duration_s": f"{result.audio_duration_s:.2f}",
                "segment_count": len(result.segments),
                "stt_elapsed_s": f"{result.elapsed_s:.2f}",
                "cli_elapsed_s": f"{response.elapsed_s:.2f}",
                "cli_success": response.success,
            })

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="realtime-panel MVP runner")
    parser.add_argument("transcript", nargs="?", type=Path, help="발화 텍스트 파일 경로 (한 줄 = 1 발화)")
    parser.add_argument("--interactive", action="store_true", help="stdin 대화형 입력")
    parser.add_argument("--wav", type=Path, default=None, help="WAV 파일 입력 (Whisper 전사 후 파이프라인)")
    parser.add_argument(
        "--model",
        default="small",
        choices=["tiny", "base", "small", "medium", "large-v3"],
        help="Whisper 모델 크기 (--wav 모드에서만 사용). 기본 small",
    )
    parser.add_argument(
        "--initial-prompt-file",
        type=Path,
        default=None,
        help="Whisper initial_prompt 파일 경로 (UTF-8). --wav 모드에서만 사용",
    )
    parser.add_argument(
        "--domain-profile",
        choices=["ai", "none"],
        default="none",
        help="Whisper 도메인 용어집 프로필. --wav 모드에서 사용",
    )
    args = parser.parse_args()

    if args.wav:
        sys.exit(run_from_wav(
            wav_path=args.wav,
            model=args.model,
            initial_prompt_file=args.initial_prompt_file,
            domain_profile=None if args.domain_profile == "none" else args.domain_profile,
        ))
    elif args.interactive:
        run_interactive()
    elif args.transcript:
        if not args.transcript.exists():
            sys.exit(f"파일을 찾을 수 없습니다: {args.transcript}")
        run_from_file(args.transcript)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
