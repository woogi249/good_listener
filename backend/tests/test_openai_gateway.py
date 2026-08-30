from types import SimpleNamespace

import pytest

from panel.openai_gateway import OpenAIGateway, OpenAIModels, OpenAIUnavailable
from panel.schemas import FactVerification, ProgressAnalysis, SourceRef


class FakeResponses:
    def __init__(self):
        self.calls = []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("tools"):
            parsed = FactVerification(
                status="supported",
                verdict="공식 문서가 주장을 뒷받침합니다.",
                sources=[SourceRef(url="https://fallback.example", title="fallback")],
            )
            output = [{"type": "web_search_call", "action": {"sources": [{"url": "https://openai.com/source", "title": "OpenAI"}]}}]
        else:
            parsed = ProgressAnalysis(current_topic="테스트", current_topic_evidence_ids=["u1"])
            output = []
        return SimpleNamespace(output_parsed=parsed, model_dump=lambda **_: {"output": output})


class FakeSecrets:
    def __init__(self):
        self.session = None

    async def create(self, *, session):
        self.session = session
        return SimpleNamespace(
            value="ephemeral",
            expires_at=123,
            model_dump=lambda **_: {"value": "ephemeral", "expires_at": 123},
        )


class FakeTranscriptions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            model_dump=lambda **_: {
                "text": "전사",
                "segments": [{"speaker": "A", "text": "전사", "start": 0, "end": 1}],
            }
        )


class FakeClient:
    def __init__(self):
        self.responses = FakeResponses()
        self.realtime = SimpleNamespace(client_secrets=FakeSecrets())
        self.audio = SimpleNamespace(transcriptions=FakeTranscriptions())


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_realtime_secret_is_transcription_only_and_uses_korean_prompt():
    client = FakeClient()
    gateway = OpenAIGateway(client=client, models=OpenAIModels(realtime="gpt-live-transcribe"))

    secret = await gateway.create_realtime_client_secret(
        topic="출시 회의", goal="결정", terms=["Good Listener"]
    )

    assert secret["value"] == "ephemeral"
    session = client.realtime.client_secrets.session
    assert session["type"] == "transcription"
    assert session["audio"]["input"]["transcription"]["model"] == "gpt-live-transcribe"
    assert session["audio"]["input"]["transcription"]["languages"] == ["ko", "en"]
    assert session["audio"]["input"]["transcription"]["delay"] == "low"
    assert session["audio"]["input"]["transcription"]["keywords"] == ["Good Listener"]
    assert "voice" not in str(session).lower()


@pytest.mark.anyio
async def test_responses_calls_disable_storage_and_fact_requires_sources():
    client = FakeClient()
    gateway = OpenAIGateway(client=client)
    progress = await gateway.analyze_progress(
        meeting={"topic": "t", "goal": "g"},
        utterances=[{"id": "u1", "speaker": "a", "text": "논의"}],
        previous_state={},
    )
    fact = await gateway.verify_public_fact(claim="공개 주장", context="문맥")

    assert progress.current_topic == "테스트"
    assert all(call["store"] is False for call in client.responses.calls)
    assert client.responses.calls[1]["tools"] == [{"type": "web_search"}]
    assert client.responses.calls[1]["include"] == ["web_search_call.action.sources"]
    assert fact.status == "supported"
    assert fact.sources[0].url == "https://openai.com/source"


@pytest.mark.anyio
async def test_missing_standard_api_key_is_explicit(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    gateway = OpenAIGateway(api_key="")
    with pytest.raises(OpenAIUnavailable):
        await gateway.create_realtime_client_secret(topic="", goal="", terms=[])


@pytest.mark.anyio
async def test_diarization_omits_prompt_that_model_does_not_support():
    client = FakeClient()
    gateway = OpenAIGateway(client=client)

    result = await gateway.transcribe_audio_chunk(
        filename="meeting.webm",
        content=b"webm",
        content_type="audio/webm",
    )

    call = client.audio.transcriptions.calls[0]
    assert call["model"] == "gpt-4o-transcribe-diarize"
    assert call["response_format"] == "diarized_json"
    assert call["chunking_strategy"] == "auto"
    assert "prompt" not in call
    assert result["segments"][0]["speaker"] == "A"
