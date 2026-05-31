import sys
import json
from types import SimpleNamespace

from panel import exaone_dispatcher as exaone


def test_exaone_requires_api_key(monkeypatch):
    monkeypatch.setattr(exaone, "_load_dotenv", lambda: None)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("EXAONE_API_KEY", raising=False)
    monkeypatch.delenv("FRIENDLI_API_KEY", raising=False)

    response = exaone.call_exaone("fact_checker", "최근 발화", timeout_s=0.1)

    assert response.provider == "exaone"
    assert response.success is False
    assert "EXAONE_API_KEY" in response.error


def test_prompt_for_triggered_call_keeps_context_and_recent_utterances():
    prompt = """## 회의 준비 문맥
- 주제: 모델 비교

당신은 테스트 에이전트입니다.

## 출력 제약
- 없으면 빈 문자열만 출력

## 최근 발화
- Claude Opus 4.8을 확인해야 합니다.

## 팩트체크:
"""

    compact = exaone._prompt_for_triggered_call(prompt)

    assert "- 주제: 모델 비교" in compact
    assert "발화: Claude Opus 4.8을 확인해야 합니다." in compact
    assert "빈 문자열" not in compact
    assert "당신은 테스트" not in compact


def test_exaone_fast_fallback_when_thinking_has_empty_content(monkeypatch):
    calls = []

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeChoice:
        def __init__(self, content):
            self.message = FakeMessage(content)

    class FakeCompletion:
        def __init__(self, content):
            self.choices = [FakeChoice(content)]

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            thinking = kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"]
            return FakeCompletion("" if thinking else "확인 필요: 모델명 검증")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(exaone, "_load_dotenv", lambda: None)
    monkeypatch.setenv("EXAONE_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    response = exaone.call_exaone("fact_checker", "## 최근 발화\n- 모델명 확인", timeout_s=10)

    assert response.success is True
    assert response.provider == "exaone:fast-fallback"
    assert response.stdout == "확인 필요: 모델명 검증"
    assert response.fallback_from == "empty response"
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True
    assert calls[0]["extra_body"]["include_reasoning"] is False
    assert calls[1]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert calls[0]["model"] == exaone.EXAONE_MODEL


def test_parse_duckduckgo_results_decodes_sources():
    html = '''
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpost">Example <b>Title</b></a>
    <div class="result__snippet">Official <b>snippet</b></div>
    '''

    results = exaone._parse_duckduckgo_results(html, limit=1)

    assert results == [
        {
            "title": "Example Title",
            "url": "https://example.com/post",
            "snippet": "Official snippet",
        }
    ]


def test_web_fact_check_uses_sources_and_exaone_without_thinking(monkeypatch):
    calls = []

    class FakeMessage:
        content = "맞음: 공식 발표와 일치"

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return FakeCompletion()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    sources = [{"title": "Source", "url": "https://example.com", "snippet": "claim"}]
    monkeypatch.setattr(exaone, "_load_dotenv", lambda: None)
    monkeypatch.setattr(exaone, "_search_web", lambda *args, **kwargs: sources)
    monkeypatch.setenv("EXAONE_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    response = exaone.call_exaone_web_fact_check("## 최근 발화\n- 테스트 주장", timeout_s=10)

    assert response.success is True
    assert response.provider == "exaone+web"
    assert response.sources == sources
    assert calls[0]["model"] == exaone.EXAONE_MODEL
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_ui_director_normalizes_layout_spec(monkeypatch):
    calls = []

    class FakeMessage:
        content = json.dumps({
            "layout_mode": "focus_b",
            "columns": [0.2, 1.4, 9],
            "panel_visuals": {
                "fact_checker": {
                    "tone": "danger",
                    "emphasis": "pulse",
                    "density": "expanded",
                    "importance": 5,
                    "urgency": 4,
                },
                "unknown": {"tone": "danger"},
            },
            "reason": "팩트체크 우선",
            "expires_after_s": 200,
        })

    class FakeChoice:
        message = FakeMessage()

    class FakeCompletion:
        choices = [FakeChoice()]

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return FakeCompletion()

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(exaone, "_load_dotenv", lambda: None)
    monkeypatch.setenv("EXAONE_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))

    response = exaone.call_exaone_ui_director("{}", timeout_s=10)
    spec = json.loads(response.stdout)

    assert response.success is True
    assert response.provider == "exaone:ui-director"
    assert spec["layout_mode"] == "focus_b"
    assert spec["columns"] == [0.4, 1.4, 1.8]
    assert spec["expires_after_s"] == 90
    assert spec["panel_visuals"]["fact_checker"]["importance"] == 3
    assert "unknown" not in spec["panel_visuals"]
    assert calls[0]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
