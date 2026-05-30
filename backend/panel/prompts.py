"""프롬프트 템플릿 로더.

`prompts/<panel_name>.md` 파일을 읽고 `{transcript}` 플레이스홀더에 롤링 버퍼 텍스트를 주입한다.
"""
from __future__ import annotations

from pathlib import Path

# backend/panel/prompts.py → 프로젝트 루트의 prompts/ 디렉토리 찾기
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = _REPO_ROOT / "prompts"


def load_prompt(panel_name: str) -> str:
    path = PROMPTS_DIR / f"{panel_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")


def render(panel_name: str, transcript: str) -> str:
    template = load_prompt(panel_name)
    return template.replace("{transcript}", transcript)
