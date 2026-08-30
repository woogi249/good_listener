"""`claude` CLI subprocess 호출 래퍼.

S-1 규약: 프롬프트는 반드시 stdin pipe로 전달. `-p "<prompt>"` 명령줄 인자
방식은 `ps aux`/`tasklist /v`로 프롬프트 내용이 노출되므로 금지.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClaudeResponse:
    success: bool
    stdout: str
    stderr: str
    elapsed_s: float
    error: str | None = None
    provider: str = "claude"
    fallback_from: str | None = None
    sources: list[dict] | None = None
    usage: dict[str, int] | None = None
    budget: dict | None = None


def _resolve_command(name: str) -> str:
    """Windows npm shims need the .cmd path when launched by subprocess."""
    return shutil.which(name) or shutil.which(f"{name}.cmd") or name


def _run_stdin_command(
    command: list[str],
    prompt: str,
    timeout_s: float,
    provider: str,
) -> ClaudeResponse:
    start = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error=f"timeout after {timeout_s}s",
            provider=provider,
        )
    except FileNotFoundError:
        return ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=0.0,
            error=f"`{provider}` CLI not found. Install + authenticate first.",
            provider=provider,
        )

    elapsed = time.perf_counter() - start
    success = result.returncode == 0
    return ClaudeResponse(
        success=success,
        stdout=(result.stdout or "").strip(),
        stderr=(result.stderr or "")[:500],
        elapsed_s=elapsed,
        error=None if success else f"returncode={result.returncode}",
        provider=provider,
    )


def call_claude(prompt: str, timeout_s: float = 15.0) -> ClaudeResponse:
    """claude CLI를 stdin pipe 방식으로 호출한다."""
    return _run_stdin_command(
        [_resolve_command("claude"), "-p", "-"],
        prompt,
        timeout_s,
        provider="claude",
    )


def call_codex(prompt: str, timeout_s: float = 15.0) -> ClaudeResponse:
    """codex CLI를 stdin pipe 방식으로 호출한다.

    `codex exec`는 상태 로그를 stderr에도 쓰므로, `--output-last-message`의
    파일 값을 우선 사용해 패널에는 최종 답변만 전달한다.
    """
    codex = _resolve_command("codex")
    fd, out_path = tempfile.mkstemp(prefix="realtime-panel-codex-", suffix=".txt")
    os.close(fd)
    try:
        response = _run_stdin_command(
            [
                codex,
                "exec",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "--sandbox",
                "read-only",
                "-o",
                out_path,
                "-",
            ],
            prompt,
            timeout_s,
            provider="codex",
        )
        if response.success:
            final_message = Path(out_path).read_text(encoding="utf-8").strip()
            if final_message:
                response.stdout = final_message
        return response
    finally:
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass


def call_claude_with_codex_fallback(
    prompt: str,
    timeout_s: float = 15.0,
) -> ClaudeResponse:
    """claude 실패 시 codex CLI로 같은 프롬프트를 재시도한다."""
    start = time.perf_counter()
    primary = call_claude(prompt, timeout_s=timeout_s)
    if primary.success:
        return primary

    fallback = call_codex(prompt, timeout_s=timeout_s)
    fallback.elapsed_s = time.perf_counter() - start
    fallback.fallback_from = primary.error or "claude failed"
    if not fallback.success:
        fallback.error = (
            f"claude failed ({primary.error}); "
            f"codex fallback failed ({fallback.error})"
        )
    return fallback


def enforce_one_line_50chars(text: str) -> str:
    """1줄/50자 제약 강제. CLI 응답이 다듬기 전이라도 안전하게 자름."""
    if not text:
        return ""
    first = text.splitlines()[0].strip()
    return first[:50]
