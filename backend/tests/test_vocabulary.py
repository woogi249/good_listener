from pathlib import Path

from panel.vocabulary import (
    build_hotwords,
    build_session_prompt,
    correct_domain_terms,
    hotwords_for_profile,
    load_domain_prompt,
    parse_terms,
)


def test_ai_domain_prompt_contains_core_terms():
    prompt = load_domain_prompt("ai")

    assert prompt is not None
    assert "Codex" in prompt
    assert "Claude" in prompt
    assert "OpenAI" in prompt


def test_ai_hotwords_contains_core_terms():
    hotwords = hotwords_for_profile("ai")

    assert hotwords is not None
    assert "GPT-5.5" in hotwords
    assert "Opus" in hotwords


def test_ai_alias_correction():
    text = "코덱스 5.5랑 클로드 오프스, 오픈 에이아이를 확인"

    corrected = correct_domain_terms(text, "ai")

    assert "Codex 5.5" in corrected
    assert "Claude Opus" in corrected
    assert "OpenAI" in corrected


def test_prompt_file_overrides_profile(tmp_path: Path):
    prompt_file = tmp_path / "domain.txt"
    prompt_file.write_text("custom terms", encoding="utf-8")

    assert load_domain_prompt("ai", prompt_file) == "custom terms"


def test_parse_terms_deduplicates_common_separators():
    assert parse_terms("Codex, Claude\nOpus; Codex") == ["Codex", "Claude", "Opus"]


def test_session_prompt_includes_topic_goal_terms():
    prompt = build_session_prompt(
        topic="AI 모델 업데이트",
        goal="기획 아이템 결정",
        terms=["Codex 5.5", "Claude Opus"],
        base_prompt="base",
    )

    assert prompt is not None
    assert "base" in prompt
    assert "AI 모델 업데이트" in prompt
    assert "Codex 5.5" in prompt


def test_build_hotwords_adds_extra_terms():
    hotwords = build_hotwords("ai", ["Spark", "Claude Opus 4.7"])

    assert hotwords is not None
    assert "Spark" in hotwords
    assert "Claude Opus 4.7" in hotwords
