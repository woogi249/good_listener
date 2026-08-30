"""EXAONE OpenAI-compatible API dispatcher."""
from __future__ import annotations

import json
import re
import os
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .budget import BudgetGate
from .cli_dispatcher import ClaudeResponse


EXAONE_BASE_URL = "https://api.friendli.ai/serverless/v1"
EXAONE_MODEL = "LGAI-EXAONE/K-EXAONE-236B-A23B"
_ROOT_DIR = Path(__file__).resolve().parent.parent.parent

_SYSTEM_PROMPTS = {
    "summarizer": (
        "당신은 한국어 회의 실시간 요약 에이전트입니다. "
        "최근 발화 흐름을 1줄 50자 이내 명사형으로 요약하세요."
    ),
    "fact_checker": (
        "당신은 한국어 회의 실시간 팩트체크 에이전트입니다. "
        "수치, 일정, 고유명사, 기술 주장을 출처 기반으로 판정하세요. "
        "답변은 '맞음:', '틀림:', '근거부족:' 중 하나로 시작하세요."
    ),
    "ideator": (
        "당신은 한국어 회의 실시간 아이디어 에이전트입니다. "
        "논의가 막히거나 반복될 때 다음 질문이나 대안을 제안하세요."
    ),
    "devils_advocate": (
        "당신은 한국어 회의 실시간 반박 에이전트입니다. "
        "합의가 빠를 때 놓친 리스크를 짧게 경고하세요."
    ),
    "parking_lot": (
        "당신은 한국어 회의 실시간 미해결 항목 에이전트입니다. "
        "아직 닫히지 않은 질문과 추후 확인 항목을 '미해결: ...' 형식으로 수집하세요."
    ),
    "action_candidate": (
        "당신은 한국어 회의 실시간 액션 후보 에이전트입니다. "
        "누가 무엇을 언제까지 해야 하는지 'TODO: ...' 형식으로 후보를 뽑으세요."
    ),
    "customer_perspective": (
        "당신은 한국어 회의 실시간 고객관점 에이전트입니다. "
        "팀 내부 관점이 강할 때 고객/사용자 입장의 질문을 던지세요."
    ),
}

_OUTPUT_RULES = (
    "최종 답변만 content에 작성하세요. 내부 추론은 출력하지 마세요. "
    "반드시 한국어 1줄, 50자 이내로 답하세요."
)
_DDG_HTML_URL = "https://duckduckgo.com/html/"
_USER_AGENT = "Mozilla/5.0 (compatible; good-listener/0.1)"
_LAYOUT_COLUMNS = {
    "normal": [0.75, 0.75, 1.0],
    "focus_a": [1.25, 0.58, 0.82],
    "focus_b": [0.58, 1.25, 0.82],
    "focus_c": [0.55, 0.55, 1.5],
    "critical": [0.44, 0.56, 1.6],
}
_LAYOUT_MODES = set(_LAYOUT_COLUMNS)
_PANEL_KEYS = {"summarizer", "fact_checker", "timeline"}
_TONES = {"neutral", "danger", "action", "customer", "opportunity", "pending"}
_EMPHASIS = {"none", "subtle", "strong", "pulse"}
_DENSITIES = {"compact", "normal", "expanded"}


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        _load_dotenv_fallback(_ROOT_DIR / ".env")
        return
    load_dotenv(_ROOT_DIR / ".env")


def _load_dotenv_fallback(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


def _prompt_for_triggered_call(prompt: str) -> str:
    context_lines = []
    utterance_lines = []
    in_recent_utterances = False

    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- 주제:", "- 목표:", "- 주요 용어:")):
            context_lines.append(stripped)
        if stripped.startswith("## 최근 발화"):
            in_recent_utterances = True
            continue
        if in_recent_utterances and stripped.startswith("## "):
            in_recent_utterances = False
        if in_recent_utterances and stripped:
            utterance = stripped[2:].strip() if stripped.startswith("- ") else stripped
            utterance_lines.append(f"발화: {utterance}")

    if not utterance_lines:
        return "\n".join(
            line
            for line in prompt.splitlines()
            if "빈 문자열" not in line
        )

    parts = []
    if context_lines:
        parts.append("## 회의 준비 문맥\n" + "\n".join(context_lines))
    parts.append("## 최근 발화\n" + "\n".join(utterance_lines))
    return "\n\n".join(parts)


def _api_key() -> str | None:
    return (
        os.getenv("EXAONE_API_KEY")
        or os.getenv("FRIENDLI_API_KEY")
        or os.getenv("API_KEY")
    )


def has_exaone_api_key() -> bool:
    _load_dotenv()
    return bool(_api_key())


def call_exaone(
    panel_name: str,
    prompt: str,
    timeout_s: float = 15.0,
    budget_gate: BudgetGate | None = None,
) -> ClaudeResponse:
    start = time.perf_counter()
    _load_dotenv()
    api_key = _api_key()
    if not api_key:
        return ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error="EXAONE_API_KEY environment variable is not set",
            provider="exaone",
        )

    try:
        from openai import OpenAI
    except ImportError:
        return ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error="openai package is not installed",
            provider="exaone",
        )

    system_prompt = _SYSTEM_PROMPTS.get(
        panel_name,
        "당신은 한국어 회의 실시간 분석 에이전트입니다.",
    )

    client = OpenAI(
        api_key=api_key,
        base_url=EXAONE_BASE_URL,
        timeout=timeout_s,
        max_retries=0,
    )
    messages = [
        {"role": "system", "content": f"{system_prompt}\n{_OUTPUT_RULES}"},
        {"role": "user", "content": _prompt_for_triggered_call(prompt)},
    ]
    response = _create_chat_completion(
        client=client,
        messages=messages,
        timeout_s=timeout_s,
        enable_thinking=True,
        provider="exaone",
        start=start,
        budget_gate=budget_gate,
        budget_feature=f"{panel_name}:thinking",
    )
    if not response.success:
        if response.stdout.startswith("예산초과:"):
            return response
        fallback = _create_chat_completion(
            client=client,
            messages=messages,
            timeout_s=min(6.0, timeout_s),
            enable_thinking=False,
            provider="exaone:fast-fallback",
            start=start,
            budget_gate=budget_gate,
            budget_feature=f"{panel_name}:fast-fallback",
        )
        if fallback.success:
            fallback.fallback_from = response.error or "exaone thinking response failed"
            return fallback
        fallback.error = (
            f"thinking failed ({response.error}); "
            f"fast fallback failed ({fallback.error})"
        )
        return fallback

    return response


def call_exaone_web_fact_check(
    prompt: str,
    timeout_s: float = 15.0,
    budget_gate: BudgetGate | None = None,
) -> ClaudeResponse:
    start = time.perf_counter()
    claim = _extract_latest_claim(prompt)
    query = _search_query_for_claim(claim)
    sources = _search_web(query, limit=4, timeout_s=min(5.0, max(2.0, timeout_s / 3)))
    if not sources:
        return ClaudeResponse(
            success=False,
            stdout="근거부족: 검색 출처를 찾지 못함",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error="no web sources",
            provider="exaone+web",
            sources=[],
        )

    _load_dotenv()
    api_key = _api_key()
    if not api_key:
        return ClaudeResponse(
            success=False,
            stdout="근거부족: API 키 없음",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error="EXAONE_API_KEY environment variable is not set",
            provider="exaone+web",
            sources=sources,
        )

    try:
        from openai import OpenAI
    except ImportError:
        return ClaudeResponse(
            success=False,
            stdout="근거부족: openai 패키지 없음",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error="openai package is not installed",
            provider="exaone+web",
            sources=sources,
        )

    client = OpenAI(
        api_key=api_key,
        base_url=EXAONE_BASE_URL,
        timeout=timeout_s,
        max_retries=0,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "당신은 출처 기반 실시간 팩트체크 에이전트입니다. "
                "주장을 검색 결과와 비교해 '맞음:', '틀림:', '근거부족:' 중 하나로만 시작하세요. "
                "출처에 없는 내용은 추측하지 마세요. "
                "최종 답변만 한국어 1줄 50자 이내로 작성하세요."
            ),
        },
        {
            "role": "user",
            "content": _render_fact_check_prompt(claim, sources),
        },
    ]
    response = _create_chat_completion(
        client=client,
        messages=messages,
        timeout_s=min(timeout_s, 10.0),
        enable_thinking=False,
        provider="exaone+web",
        start=start,
        budget_gate=budget_gate,
        budget_feature="fact_checker:web",
    )
    response.sources = sources
    if response.success:
        response.stdout = _normalize_fact_check_verdict(response.stdout)
        return response

    response.stdout = "근거부족: 검색 결과로 직접 판정 불가"
    response.sources = sources
    return response


def call_exaone_ui_director(
    prompt: str,
    timeout_s: float = 8.0,
    budget_gate: BudgetGate | None = None,
) -> ClaudeResponse:
    start = time.perf_counter()
    _load_dotenv()
    api_key = _api_key()
    if not api_key:
        return ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error="EXAONE_API_KEY environment variable is not set",
            provider="exaone:ui-director",
        )

    try:
        from openai import OpenAI
    except ImportError:
        return ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=time.perf_counter() - start,
            error="openai package is not installed",
            provider="exaone:ui-director",
        )

    client = OpenAI(
        api_key=api_key,
        base_url=EXAONE_BASE_URL,
        timeout=timeout_s,
        max_retries=0,
    )
    messages = [
        {
            "role": "system",
            "content": (
                "당신은 good-listener의 ggui UI Director입니다. "
                "회의 중 사용자가 놓치면 안 되는 정보를 기준으로 A/B/C 패널의 크기와 강조를 결정하세요. "
                "글자 크기는 절대 키우지 말고, 허용된 JSON 값만 반환하세요. "
                "반드시 JSON 객체만 출력하세요. 마크다운과 설명은 금지입니다. "
                "layout_mode는 normal, focus_a, focus_b, focus_c, critical 중 하나입니다. "
                "columns는 숫자 3개 배열이며 A/B/C 비율입니다. "
                "panel_visuals 키는 summarizer, fact_checker, timeline만 허용됩니다. "
                "각 panel_visual은 tone, emphasis, density, importance, urgency를 가질 수 있습니다."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    response = _create_chat_completion(
        client=client,
        messages=messages,
        timeout_s=min(timeout_s, 8.0),
        enable_thinking=False,
        provider="exaone:ui-director",
        start=start,
        budget_gate=budget_gate,
        budget_feature="ui_director",
    )
    if not response.success:
        return response

    try:
        spec = _normalize_ui_layout_spec(response.stdout)
    except ValueError as exc:
        response.success = False
        response.stdout = ""
        response.error = str(exc)
        return response

    response.stdout = json.dumps(spec, ensure_ascii=False)
    return response


def _create_chat_completion(
    client,
    messages: list[dict[str, str]],
    timeout_s: float,
    enable_thinking: bool,
    provider: str,
    start: float,
    budget_gate: BudgetGate | None = None,
    budget_feature: str = "exaone",
) -> ClaudeResponse:
    reservation = None
    if budget_gate is not None:
        decision = budget_gate.reserve_messages(messages, feature=budget_feature)
        if not decision.allowed:
            return ClaudeResponse(
                success=False,
                stdout="예산초과: 분석 생략",
                stderr="",
                elapsed_s=time.perf_counter() - start,
                error=decision.reason,
                provider=provider,
                budget=budget_gate.state(),
            )
        reservation = decision.reservation

    try:
        completion = client.chat.completions.create(
            model=EXAONE_MODEL,
            temperature=0.2,
            timeout=timeout_s,
            extra_body={
                "parse_reasoning": True,
                "include_reasoning": False,
                "chat_template_kwargs": {
                    "enable_thinking": enable_thinking,
                },
            },
            messages=messages,
        )
    except Exception as exc:
        return ClaudeResponse(
            success=False,
            stdout="",
            stderr=str(exc)[:500],
            elapsed_s=time.perf_counter() - start,
            error=f"{type(exc).__name__}: {exc}",
            provider=provider,
            budget=budget_gate.state() if budget_gate is not None else None,
        )

    message = completion.choices[0].message if completion.choices else None
    content = (getattr(message, "content", "") or "").strip()
    usage = _usage_dict(getattr(completion, "usage", None))
    if budget_gate is not None:
        budget_gate.settle(reservation, usage)
    return ClaudeResponse(
        success=bool(content),
        stdout=content,
        stderr="",
        elapsed_s=time.perf_counter() - start,
        error=None if content else "empty response",
        provider=provider,
        usage=usage,
        budget=budget_gate.state() if budget_gate is not None else None,
    )


def _usage_dict(usage: object) -> dict[str, int] | None:
    if usage is None:
        return None
    values: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is None and isinstance(usage, dict):
            value = usage.get(key)
        if value is not None:
            try:
                values[key] = int(value)
            except (TypeError, ValueError):
                pass
    return values or None


def _extract_latest_claim(prompt: str) -> str:
    compact = _prompt_for_triggered_call(prompt)
    utterances = []
    for line in compact.splitlines():
        stripped = line.strip()
        if stripped.startswith("발화:"):
            utterances.append(stripped.removeprefix("발화:").strip())
    if utterances:
        return utterances[-1]
    return " ".join(line.strip() for line in compact.splitlines() if line.strip())[:240]


def _search_query_for_claim(claim: str) -> str:
    ascii_tokens = re.findall(r"[A-Za-z][A-Za-z0-9.+-]*|\d+(?:\.\d+)?%?", claim)
    if len(ascii_tokens) >= 2:
        query = " ".join(ascii_tokens[:10])
        return f"{query} official benchmark source"
    query = re.sub(r"\s+", " ", claim.strip())
    return f"{query} 공식 발표 출처"


def _search_web(query: str, limit: int = 4, timeout_s: float = 5.0) -> list[dict]:
    try:
        import httpx

        response = httpx.get(
            _DDG_HTML_URL,
            params={"q": query},
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout_s,
            follow_redirects=True,
        )
        response.raise_for_status()
    except Exception:
        return []

    return _parse_duckduckgo_results(response.text, limit=limit)


def _parse_duckduckgo_results(html: str, limit: int = 4) -> list[dict]:
    title_matches = list(
        re.finditer(
            r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>',
            html,
            flags=re.S,
        )
    )
    snippet_matches = list(
        re.finditer(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|div)>',
            html,
            flags=re.S,
        )
    )
    results = []
    seen_urls = set()
    for index, match in enumerate(title_matches):
        url = _decode_duckduckgo_url(unescape(match.group(1)))
        if not url.startswith(("http://", "https://")) or url in seen_urls:
            continue
        title = _clean_html(match.group(2))
        snippet = ""
        if index < len(snippet_matches):
            snippet = _clean_html(snippet_matches[index].group(1))
        seen_urls.add(url)
        results.append({
            "title": title[:120],
            "url": url,
            "snippet": snippet[:240],
        })
        if len(results) >= limit:
            break
    return results


def _decode_duckduckgo_url(url: str) -> str:
    if url.startswith("//"):
        url = f"https:{url}"
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and "uddg" in parsed.query:
        return unquote(parse_qs(parsed.query).get("uddg", [url])[0])
    return url


def _clean_html(value: str) -> str:
    text = re.sub(r"<.*?>", " ", value)
    return " ".join(unescape(text).split())


def _render_fact_check_prompt(claim: str, sources: list[dict]) -> str:
    source_lines = []
    for index, source in enumerate(sources, start=1):
        source_lines.append(
            f"[{index}] {source['title']}\n"
            f"URL: {source['url']}\n"
            f"요약: {source.get('snippet', '')}"
        )
    return (
        f"## 주장\n{claim}\n\n"
        "## 검색 출처\n"
        + "\n\n".join(source_lines)
        + "\n\n## 판정\n출처만 근거로 1줄 판정:"
    )


def _normalize_fact_check_verdict(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith(("맞음:", "틀림:", "근거부족:")):
        return cleaned
    if "거짓" in cleaned or "틀림" in cleaned:
        return f"틀림: {cleaned}"[:50]
    if "맞" in cleaned or "사실" in cleaned:
        return f"맞음: {cleaned}"[:50]
    return f"근거부족: {cleaned}"[:50]


def _normalize_ui_layout_spec(raw: str | dict) -> dict:
    if isinstance(raw, str):
        try:
            value = json.loads(_extract_json_object(raw))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid ui layout json: {exc}") from exc
    elif isinstance(raw, dict):
        value = raw
    else:
        raise ValueError("ui layout spec must be an object")

    if not isinstance(value, dict):
        raise ValueError("ui layout spec must be an object")

    mode = str(value.get("layout_mode") or value.get("mode") or "normal").strip()
    if mode not in _LAYOUT_MODES:
        mode = "normal"

    columns = _normalize_columns(value.get("columns"), mode)
    panel_visuals = _normalize_panel_visuals(value.get("panel_visuals") or {})
    reason = str(value.get("reason") or "").strip()[:90]
    expires_after_s = _clamp_int(value.get("expires_after_s"), 8, 5, 90)

    return {
        "runtime": "ggui-compatible",
        "component": "workspace-layout",
        "layout_mode": mode,
        "columns": columns,
        "panel_visuals": panel_visuals,
        "reason": reason,
        "expires_after_s": expires_after_s,
    }


def _extract_json_object(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("ui layout response has no json object")
    return text[start : end + 1]


def _normalize_columns(raw: object, mode: str) -> list[float]:
    default = _LAYOUT_COLUMNS[mode]
    if not isinstance(raw, list) or len(raw) != 3:
        return default
    columns: list[float] = []
    for index, value in enumerate(raw):
        try:
            columns.append(round(max(0.4, min(1.8, float(value))), 2))
        except (TypeError, ValueError):
            columns.append(default[index])
    return columns


def _normalize_panel_visuals(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}

    visuals = {}
    for key, value in raw.items():
        panel = str(key).strip()
        if panel in {"c", "c_plus", "insight_feed"}:
            panel = "timeline"
        if panel not in _PANEL_KEYS or not isinstance(value, dict):
            continue

        tone = str(value.get("tone") or "neutral").strip()
        emphasis = str(value.get("emphasis") or "none").strip()
        density = str(value.get("density") or "normal").strip()
        visuals[panel] = {
            "runtime": "ggui-compatible",
            "component": "primary-panel" if panel != "timeline" else "timeline-panel",
            "panel": panel,
            "tone": tone if tone in _TONES else "neutral",
            "emphasis": emphasis if emphasis in _EMPHASIS else "none",
            "density": density if density in _DENSITIES else "normal",
            "importance": _clamp_int(value.get("importance"), 1, 1, 3),
            "urgency": _clamp_int(value.get("urgency"), 1, 1, 3),
            "reason": str(value.get("reason") or "").strip()[:80],
        }
    return visuals


def _clamp_int(value: object, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))
