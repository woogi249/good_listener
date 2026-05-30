"""STT 도메인 용어 힌트와 오인식 보정."""
from __future__ import annotations

import re
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = ROOT_DIR / "prompts"

AI_TERMS = (
    "OpenAI",
    "ChatGPT",
    "GPT-5.5",
    "GPT-5",
    "GPT-4.1",
    "Codex",
    "Claude",
    "Claude Code",
    "Sonnet",
    "Opus",
    "Anthropic",
    "Gemini",
    "DeepMind",
    "Google",
    "Microsoft",
    "Copilot",
    "Cursor",
    "OpenCode",
    "VS Code",
    "API",
    "CLI",
    "MCP",
    "AI agent",
    "LLM",
)

_TERM_SPLIT_PATTERN = re.compile(r"[,;\n]+")

_AI_ALIASES = (
    (r"코덱스", "Codex"),
    (r"코텍스", "Codex"),
    (r"클로드", "Claude"),
    (r"클라우드", "Claude"),
    (r"소넷", "Sonnet"),
    (r"선넷", "Sonnet"),
    (r"오프스", "Opus"),
    (r"옵스", "Opus"),
    (r"오픈\s?에이아이", "OpenAI"),
    (r"오픈\s?AI", "OpenAI"),
    (r"챗\s?지피티", "ChatGPT"),
    (r"챗\s?GPT", "ChatGPT"),
    (r"채찍\s?피티", "ChatGPT"),
    (r"지피티", "GPT"),
    (r"제미나이", "Gemini"),
    (r"재미나이", "Gemini"),
    (r"앤트로픽", "Anthropic"),
    (r"안트로픽", "Anthropic"),
    (r"마이크로소프트", "Microsoft"),
    (r"미소셉", "Microsoft"),
    (r"코파일럿", "Copilot"),
    (r"커서", "Cursor"),
    (r"오픈\s?코드", "OpenCode"),
    (r"브이에스\s?코드", "VS Code"),
    (r"에이피아이", "API"),
    (r"씨엘아이", "CLI"),
    (r"엠씨피", "MCP"),
    (r"엘엘엠", "LLM"),
)
_AI_ALIAS_PATTERNS = [
    (re.compile(pattern, re.IGNORECASE), replacement)
    for pattern, replacement in _AI_ALIASES
]


def load_domain_prompt(
    profile: str | None = "ai",
    prompt_file: Path | None = None,
) -> str | None:
    """Whisper initial_prompt로 사용할 도메인 힌트를 로드한다."""
    if prompt_file is not None:
        return prompt_file.read_text(encoding="utf-8").strip() or None
    if profile in (None, "", "none"):
        return None
    if profile != "ai":
        raise ValueError(f"unknown domain profile: {profile}")
    path = PROMPTS_DIR / "domain-ai.txt"
    return path.read_text(encoding="utf-8").strip()


def hotwords_for_profile(profile: str | None = "ai") -> str | None:
    if profile in (None, "", "none"):
        return None
    if profile != "ai":
        raise ValueError(f"unknown domain profile: {profile}")
    return " ".join(AI_TERMS)


def parse_terms(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """사용자가 입력한 키워드/고유명사를 정리한다."""
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates = _TERM_SPLIT_PATTERN.split(raw)
    else:
        candidates = list(raw)
    terms: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        term = str(candidate).strip()
        if not term or term in seen:
            continue
        seen.add(term)
        terms.append(term)
    return terms


def build_session_prompt(
    topic: str = "",
    goal: str = "",
    terms: list[str] | None = None,
    base_prompt: str | None = None,
) -> str | None:
    """회의 시작 전 문맥을 Whisper initial_prompt로 합친다."""
    parts: list[str] = []
    if base_prompt:
        parts.append(base_prompt.strip())
    if topic.strip():
        parts.append(f"오늘 회의 주제: {topic.strip()}")
    if goal.strip():
        parts.append(f"회의 목표: {goal.strip()}")
    cleaned_terms = parse_terms(terms)
    if cleaned_terms:
        parts.append("중요 고유명사와 용어: " + ", ".join(cleaned_terms))
    return " ".join(parts) or None


def build_hotwords(
    profile: str | None = "ai",
    extra_terms: list[str] | None = None,
) -> str | None:
    terms: list[str] = []
    base = hotwords_for_profile(profile)
    if base:
        terms.extend(base.split())
    terms.extend(parse_terms(extra_terms))
    return " ".join(terms) if terms else None


def correct_domain_terms(text: str, profile: str | None = "ai") -> str:
    """도메인별로 흔한 STT 오인식을 보정한다."""
    if profile in (None, "", "none"):
        return text
    if profile != "ai":
        raise ValueError(f"unknown domain profile: {profile}")
    corrected = text
    for pattern, replacement in _AI_ALIAS_PATTERNS:
        corrected = pattern.sub(replacement, corrected)
    return corrected
