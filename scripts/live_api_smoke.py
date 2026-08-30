"""Opt-in OpenAI connectivity smoke test.

This sends only a fixed synthetic sentence. It never reads meeting data.
"""
from __future__ import annotations

import os
import sys
import time


def main() -> int:
    if os.environ.get("GOOD_LISTENER_RUN_LIVE_TESTS") != "1":
        print("SKIP: set GOOD_LISTENER_RUN_LIVE_TESTS=1 to allow a billable API call")
        return 0
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is required", file=sys.stderr)
        return 2

    from openai import OpenAI

    model = os.environ.get("OPENAI_ANALYSIS_MODEL", "gpt-5.6-luna")
    started = time.perf_counter()
    response = OpenAI().responses.create(
        model=model,
        input="연결 점검입니다. 반드시 '연결 정상'만 출력하세요.",
        max_output_tokens=32,
    )
    text = (response.output_text or "").strip()
    if not text:
        print("ERROR: OpenAI returned an empty response", file=sys.stderr)
        return 3

    elapsed = time.perf_counter() - started
    print(f"OK: OpenAI Responses API · model={model} · elapsed={elapsed:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
