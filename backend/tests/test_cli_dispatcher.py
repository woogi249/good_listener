from unittest.mock import patch

from panel.cli_dispatcher import (
    ClaudeResponse,
    call_claude_with_codex_fallback,
    enforce_one_line_50chars,
)


def test_enforce_one_line_50chars_truncates_first_line():
    text = "가" * 60 + "\nsecond line"

    assert enforce_one_line_50chars(text) == "가" * 50


def test_fallback_is_not_called_when_claude_succeeds():
    primary = ClaudeResponse(
        success=True,
        stdout="ok",
        stderr="",
        elapsed_s=1.0,
        provider="claude",
    )

    with (
        patch("panel.cli_dispatcher.call_claude", return_value=primary),
        patch("panel.cli_dispatcher.call_codex") as call_codex,
    ):
        response = call_claude_with_codex_fallback("prompt")

    assert response is primary
    call_codex.assert_not_called()


def test_codex_is_used_when_claude_fails():
    primary = ClaudeResponse(
        success=False,
        stdout="",
        stderr="",
        elapsed_s=1.0,
        error="returncode=1",
        provider="claude",
    )
    fallback = ClaudeResponse(
        success=True,
        stdout="fallback ok",
        stderr="",
        elapsed_s=1.0,
        provider="codex",
    )

    with (
        patch("panel.cli_dispatcher.call_claude", return_value=primary),
        patch("panel.cli_dispatcher.call_codex", return_value=fallback) as call_codex,
    ):
        response = call_claude_with_codex_fallback("prompt")

    assert response.success is True
    assert response.provider == "codex"
    assert response.fallback_from == "returncode=1"
    call_codex.assert_called_once_with("prompt", timeout_s=15.0)
