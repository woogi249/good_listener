import asyncio
from pathlib import Path

import pytest

from panel.crypto import EncryptionManager
from panel.meeting_service import MeetingService
from panel.schemas import (
    DecisionItem,
    FactVerification,
    MinutesDraft,
    ProgressAnalysis,
    SourceRef,
    TranscriptFinal,
)
from panel.storage import SQLiteStore


class FakeGateway:
    configured = True

    def __init__(self):
        self.fact_started = asyncio.Event()
        self.fact_release = asyncio.Event()
        self.fact_calls = 0
        self.minutes_calls = 0
        self.transcription_calls = []
        self.transcription_result = {"segments": []}

    async def analyze_progress(self, *, meeting, utterances, previous_state):
        latest = utterances[-1]
        return ProgressAnalysis(
            current_topic="OpenAI 전환",
            current_topic_evidence_ids=[latest["id"]],
        )

    async def verify_public_fact(self, *, claim, context):
        self.fact_calls += 1
        self.fact_started.set()
        await self.fact_release.wait()
        return FactVerification(
            status="supported",
            verdict="공개 출처로 확인됨",
            sources=[SourceRef(url="https://example.com", title="공식")],
        )

    async def generate_minutes(self, *, meeting, utterances, snapshot, claims):
        self.minutes_calls += 1
        evidence = [utterances[-1]["id"]] if utterances else []
        return MinutesDraft(
            title=meeting["topic"] or "회의록",
            summary="테스트 회의 요약",
            decisions=[
                DecisionItem(
                    id="d1",
                    text="OpenAI 전환",
                    status="candidate",
                    evidence_utterance_ids=evidence,
                )
            ],
        )

    async def transcribe_audio_chunk(self, **kwargs):
        self.transcription_calls.append(kwargs)
        return self.transcription_result


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_fact_search_never_blocks_new_utterance_and_stop_is_idempotent(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"s" * 32))
    gateway = FakeGateway()
    events = []

    async def sink(event):
        events.append(event)

    service = MeetingService(
        store=store,
        gateway=gateway,
        audio_dir=tmp_path / "audio",
        event_sink=sink,
        progress_interval_s=0,
    )
    await service.start_workers()
    try:
        meeting = service.create_meeting(
            topic="제품 회의",
            goal="전환 결정",
            terms=[],
            consent_external_processing=True,
        )["meeting"]
        await service.start_meeting(meeting["id"])
        await service.ingest_final(
            meeting["id"],
            TranscriptFinal(item_id="u1", text="한국은행 기준금리는 3.5%입니다"),
        )
        await asyncio.wait_for(gateway.fact_started.wait(), timeout=2)

        _, inserted = await asyncio.wait_for(
            service.ingest_final(
                meeting["id"], TranscriptFinal(item_id="u2", text="다음 안건을 논의합니다")
            ),
            timeout=0.3,
        )
        assert inserted is True
        assert len(store.list_utterances(meeting["id"])) == 2

        gateway.fact_release.set()
        await _wait_until(
            lambda: store.list_claims(meeting["id"])[0]["status"] == "supported"
        )
        first_stop = await service.stop_meeting(meeting["id"])
        second_stop = await service.stop_meeting(meeting["id"])
        assert first_stop["lifecycle"] == "finalizing"
        assert second_stop["lifecycle"] in {"finalizing", "review"}
        await _wait_until(lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review")
        assert gateway.minutes_calls == 1
        assert store.get_minutes(meeting["id"])["structured"]["summary"] == "테스트 회의 요약"
        assert all(
            {"meeting_id", "event_id", "seq", "revision", "occurred_at"} <= set(event)
            for event in events
        )
    finally:
        await service.stop_workers()
        store.close()


@pytest.mark.anyio
async def test_internal_fact_is_not_sent_to_public_web_search(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"i" * 32))
    gateway = FakeGateway()
    service = MeetingService(store=store, gateway=gateway, audio_dir=tmp_path / "audio")
    meeting = service.create_meeting(
        topic="내부 회의", goal="", terms=[], consent_external_processing=True
    )["meeting"]
    await service.start_meeting(meeting["id"])

    await service.ingest_final(
        meeting["id"],
        TranscriptFinal(item_id="private", text="우리 팀 이번 분기 매출은 10억원입니다"),
    )
    claims = store.list_claims(meeting["id"])

    assert claims[0]["status"] == "internal_source_required"
    assert gateway.fact_calls == 0
    store.close()


@pytest.mark.anyio
async def test_openai_previous_item_id_reconciles_out_of_order_completion(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"o" * 32))
    service = MeetingService(store=store, gateway=FakeGateway(), audio_dir=tmp_path / "audio")
    meeting = service.create_meeting(
        topic="순서", goal="", terms=[], consent_external_processing=True
    )["meeting"]
    await service.start_meeting(meeting["id"])
    await service.ingest_final(
        meeting["id"],
        TranscriptFinal(item_id="child", previous_item_id="parent", text="두 번째 발화"),
    )
    await service.ingest_final(
        meeting["id"], TranscriptFinal(item_id="parent", text="첫 번째 발화")
    )

    assert [item["external_item_id"] for item in service.snapshot(meeting["id"])["transcript"]] == [
        "parent",
        "child",
    ]
    store.close()


@pytest.mark.anyio
async def test_diarization_decrypts_and_joins_all_chunks_before_one_openai_call(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"d" * 32))
    gateway = FakeGateway()
    gateway.transcription_result = {
        "segments": [
            {"speaker": "A", "start": 0.0, "end": 1.2, "text": "첫 번째 발화"},
            {"speaker": "B", "start": 1.3, "end": 2.5, "text": "두 번째 발화"},
        ]
    }
    service = MeetingService(store=store, gateway=gateway, audio_dir=tmp_path / "audio")
    meeting = service.create_meeting(
        topic="결합 전사", goal="", terms=["Good Listener"], consent_external_processing=True
    )["meeting"]
    await service.start_meeting(meeting["id"])
    await service.ingest_final(
        meeting["id"], TranscriptFinal(item_id="live-1", text="실시간 자막")
    )
    await service.persist_audio_chunk(
        meeting["id"],
        chunk_id="chunk-1",
        sequence=1,
        content_type="audio/webm;codecs=opus",
        content=b"webm-header-and-cluster",
    )
    await service.persist_audio_chunk(
        meeting["id"],
        chunk_id="chunk-2",
        sequence=2,
        content_type="audio/webm;codecs=opus",
        content=b"next-cluster",
    )

    diarized, warnings = await service._diarize_persisted_audio(meeting)

    assert warnings == []
    assert [item["speaker"] for item in diarized] == ["A", "B"]
    assert len(gateway.transcription_calls) == 1
    assert gateway.transcription_calls[0]["filename"] == "meeting.webm"
    assert gateway.transcription_calls[0]["content"] == b"webm-header-and-clusternext-cluster"
    sources = [item["source"] for item in service.snapshot(meeting["id"])["transcript"]]
    assert len(set(sources)) == 1
    assert sources[0].startswith("diarized:")
    store.close()


@pytest.mark.anyio
async def test_diarization_falls_back_to_live_transcript_when_sequence_has_gap(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"g" * 32))
    gateway = FakeGateway()
    service = MeetingService(store=store, gateway=gateway, audio_dir=tmp_path / "audio")
    meeting = service.create_meeting(
        topic="누락 전사", goal="", terms=[], consent_external_processing=True
    )["meeting"]
    await service.start_meeting(meeting["id"])
    await service.ingest_final(
        meeting["id"], TranscriptFinal(item_id="live-1", text="보존되는 실시간 자막")
    )
    for sequence in (1, 3):
        await service.persist_audio_chunk(
            meeting["id"],
            chunk_id=f"chunk-{sequence}",
            sequence=sequence,
            content_type="audio/webm",
            content=f"chunk-{sequence}".encode(),
        )

    diarized, warnings = await service._diarize_persisted_audio(meeting)

    assert diarized == []
    assert "누락" in warnings[0]
    assert gateway.transcription_calls == []
    assert service.snapshot(meeting["id"])["transcript"][0]["text"] == "보존되는 실시간 자막"
    store.close()


@pytest.mark.anyio
async def test_late_progress_result_is_discarded_and_latest_revision_is_reanalyzed(tmp_path: Path):
    class BlockingProgressGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.progress_started = asyncio.Event()
            self.progress_release = asyncio.Event()
            self.progress_calls = 0

        async def analyze_progress(self, *, meeting, utterances, previous_state):
            self.progress_calls += 1
            latest = utterances[-1]
            if self.progress_calls == 1:
                self.progress_started.set()
                await self.progress_release.wait()
            return ProgressAnalysis(
                current_topic=latest["text"],
                current_topic_evidence_ids=[latest["id"]],
            )

    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"p" * 32))
    gateway = BlockingProgressGateway()
    service = MeetingService(
        store=store,
        gateway=gateway,
        audio_dir=tmp_path / "audio",
        progress_interval_s=999,
    )
    await service.start_workers()
    try:
        meeting = service.create_meeting(
            topic="리비전", goal="", terms=[], consent_external_processing=True
        )["meeting"]
        await service.start_meeting(meeting["id"])
        await service.ingest_final(
            meeting["id"], TranscriptFinal(item_id="u1", text="이전 발화")
        )
        await asyncio.wait_for(gateway.progress_started.wait(), timeout=2)
        await service.ingest_final(
            meeting["id"], TranscriptFinal(item_id="u2", text="최신 발화")
        )
        gateway.progress_release.set()

        await _wait_until(
            lambda: (store.latest_snapshot(meeting["id"]) or {}).get("base_revision") == 2
        )
        snapshot = store.latest_snapshot(meeting["id"])
        assert snapshot["data"]["current_topic"] == "최신 발화"
        assert gateway.progress_calls == 2
    finally:
        await service.stop_workers()
        store.close()


@pytest.mark.anyio
async def test_late_transcript_during_minutes_generation_requeues_finalization(tmp_path: Path):
    class BlockingMinutesGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.minutes_started = asyncio.Event()
            self.minutes_release = asyncio.Event()

        async def generate_minutes(self, *, meeting, utterances, snapshot, claims):
            self.minutes_calls += 1
            latest = utterances[-1]
            if self.minutes_calls == 1:
                self.minutes_started.set()
                await self.minutes_release.wait()
            return MinutesDraft(title="회의록", summary=latest["text"])

    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"m" * 32))
    gateway = BlockingMinutesGateway()
    service = MeetingService(
        store=store,
        gateway=gateway,
        audio_dir=tmp_path / "audio",
        progress_interval_s=999,
    )
    await service.start_workers()
    try:
        meeting = service.create_meeting(
            topic="종료 경쟁", goal="", terms=[], consent_external_processing=True
        )["meeting"]
        await service.start_meeting(meeting["id"])
        await service.ingest_final(
            meeting["id"], TranscriptFinal(item_id="u1", text="먼저 들어온 발화")
        )
        await service.stop_meeting(meeting["id"])
        await asyncio.wait_for(gateway.minutes_started.wait(), timeout=2)
        await service.ingest_final(
            meeting["id"], TranscriptFinal(item_id="u2", text="종료 직전 늦게 도착한 발화")
        )
        gateway.minutes_release.set()

        await _wait_until(lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review")
        assert gateway.minutes_calls == 2
        assert store.get_minutes(meeting["id"])["structured"]["summary"] == (
            "종료 직전 늦게 도착한 발화"
        )
        assert store.get_minutes(meeting["id"])["transcript_revision"] == 2
    finally:
        await service.stop_workers()
        store.close()


@pytest.mark.anyio
async def test_late_fact_result_invalidates_and_regenerates_minutes(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"f" * 32))
    gateway = FakeGateway()
    service = MeetingService(
        store=store,
        gateway=gateway,
        audio_dir=tmp_path / "audio",
        progress_interval_s=999,
    )
    service.finalize_fact_wait_s = 0
    await service.start_workers()
    try:
        meeting = service.create_meeting(
            topic="팩트 지연", goal="", terms=[], consent_external_processing=True
        )["meeting"]
        await service.start_meeting(meeting["id"])
        await service.ingest_final(
            meeting["id"],
            TranscriptFinal(item_id="claim", text="한국은행 기준금리는 3.5%입니다"),
        )
        await asyncio.wait_for(gateway.fact_started.wait(), timeout=2)
        await service.stop_meeting(meeting["id"])

        await _wait_until(lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review")
        first_minutes = store.get_minutes(meeting["id"])
        assert first_minutes["structured"]["facts"][0]["status"] == "insufficient"

        gateway.fact_release.set()
        await _wait_until(
            lambda: store.list_claims(meeting["id"])[0]["status"] == "supported"
        )
        await _wait_until(
            lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review"
            and gateway.minutes_calls >= 2
        )

        regenerated = store.get_minutes(meeting["id"])
        assert regenerated["structured"]["facts"][0]["status"] == "supported"
        assert regenerated["structured"]["_source"]["fact_manifest_sha256"] != (
            first_minutes["structured"]["_source"]["fact_manifest_sha256"]
        )
        assert any(event["type"] == "minutes.stale" for event in store.events_after(meeting["id"]))
        approved = await service.approve_minutes(meeting["id"])
        assert approved["status"] == "approved"
    finally:
        gateway.fact_release.set()
        await service.stop_workers()
        store.close()


@pytest.mark.anyio
async def test_approval_rejects_minutes_after_fact_source_changes(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"a" * 32))
    gateway = FakeGateway()
    gateway.fact_release.set()
    service = MeetingService(store=store, gateway=gateway, audio_dir=tmp_path / "audio")
    await service.start_workers()
    try:
        meeting = service.create_meeting(
            topic="승인 경쟁", goal="", terms=[], consent_external_processing=True
        )["meeting"]
        await service.start_meeting(meeting["id"])
        await service.ingest_final(
            meeting["id"], TranscriptFinal(item_id="claim", text="기준금리는 3.5%입니다")
        )
        await _wait_until(
            lambda: store.list_claims(meeting["id"])[0]["status"] == "supported"
        )
        await service.stop_meeting(meeting["id"])
        await _wait_until(lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review")

        claim = store.list_claims(meeting["id"])[0]
        store.update_claim(claim["id"], status="contradicted", verdict="새 근거")

        with pytest.raises(ValueError, match="sources changed"):
            await service.approve_minutes(meeting["id"])
        assert store.get_meeting(meeting["id"])["lifecycle"] == "review"
        assert store.get_minutes(meeting["id"])["status"] == "draft"
    finally:
        await service.stop_workers()
        store.close()


@pytest.mark.anyio
async def test_late_audio_reopens_approved_minutes_and_uses_latest_manifest_only(tmp_path: Path):
    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"l" * 32))
    gateway = FakeGateway()
    gateway.transcription_result = {
        "segments": [{"speaker": "A", "start": 0, "end": 1, "text": "첫 오디오"}]
    }
    service = MeetingService(store=store, gateway=gateway, audio_dir=tmp_path / "audio")
    await service.start_workers()
    try:
        meeting = service.create_meeting(
            topic="오디오 지연", goal="", terms=[], consent_external_processing=True
        )["meeting"]
        await service.start_meeting(meeting["id"])
        await service.ingest_final(
            meeting["id"], TranscriptFinal(item_id="live", text="실시간 자막")
        )
        await service.persist_audio_chunk(
            meeting["id"],
            chunk_id="chunk-1",
            sequence=1,
            content_type="audio/webm",
            content=b"first",
        )
        await service.stop_meeting(meeting["id"])
        await _wait_until(lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review")
        first_minutes = store.get_minutes(meeting["id"])
        first_source = service.snapshot(meeting["id"])["transcript"][0]["source"]
        assert first_source.startswith("diarized:")
        await service.approve_minutes(meeting["id"])
        assert store.get_meeting(meeting["id"])["lifecycle"] == "completed"

        gateway.transcription_result = {
            "segments": [
                {"speaker": "A", "start": 0, "end": 1, "text": "최신 첫 발화"},
                {"speaker": "B", "start": 1, "end": 2, "text": "최신 둘째 발화"},
            ]
        }
        metadata, inserted = await service.persist_audio_chunk(
            meeting["id"],
            chunk_id="chunk-2",
            sequence=2,
            content_type="audio/webm",
            content=b"second",
        )

        assert inserted is True
        assert metadata["lifecycle"] == "finalizing"
        assert metadata["minutes_stale"] is True
        assert metadata["regeneration_started"] is True
        assert store.get_minutes(meeting["id"])["status"] == "stale"
        assert store.get_minutes(meeting["id"])["approved_at"] is None
        with pytest.raises(ValueError, match="review state"):
            await service.approve_minutes(meeting["id"])

        await _wait_until(
            lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review"
            and gateway.minutes_calls >= 2
        )
        transcript = service.snapshot(meeting["id"])["transcript"]
        assert [item["text"] for item in transcript] == ["최신 첫 발화", "최신 둘째 발화"]
        latest_sources = {item["source"] for item in transcript}
        assert len(latest_sources) == 1
        latest_source = latest_sources.pop()
        assert latest_source != first_source
        regenerated = store.get_minutes(meeting["id"])
        latest_manifest = regenerated["structured"]["_source"]["audio_manifest_sha256"]
        assert latest_manifest != first_minutes["structured"]["_source"]["audio_manifest_sha256"]
        assert latest_source.startswith(f"diarized:{latest_manifest}:")
        assert regenerated["status"] == "draft"
    finally:
        await service.stop_workers()
        store.close()


@pytest.mark.anyio
async def test_audio_arriving_during_diarization_discards_stale_result(tmp_path: Path):
    class BlockingTranscriptionGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.transcription_started = asyncio.Event()
            self.transcription_release = asyncio.Event()

        async def transcribe_audio_chunk(self, **kwargs):
            self.transcription_calls.append(kwargs)
            if len(self.transcription_calls) == 1:
                self.transcription_started.set()
                await self.transcription_release.wait()
                return {"segments": [{"speaker": "A", "text": "폐기할 이전 전사"}]}
            return {"segments": [{"speaker": "B", "text": "최신 전체 전사"}]}

    store = SQLiteStore(tmp_path / "db.sqlite", cipher=EncryptionManager(b"r" * 32))
    gateway = BlockingTranscriptionGateway()
    service = MeetingService(store=store, gateway=gateway, audio_dir=tmp_path / "audio")
    await service.start_workers()
    try:
        meeting = service.create_meeting(
            topic="전사 중 오디오", goal="", terms=[], consent_external_processing=True
        )["meeting"]
        await service.start_meeting(meeting["id"])
        await service.ingest_final(
            meeting["id"], TranscriptFinal(item_id="live", text="실시간 자막")
        )
        await service.persist_audio_chunk(
            meeting["id"],
            chunk_id="chunk-1",
            sequence=1,
            content_type="audio/webm",
            content=b"first",
        )
        await service.stop_meeting(meeting["id"])
        await asyncio.wait_for(gateway.transcription_started.wait(), timeout=2)

        await service.persist_audio_chunk(
            meeting["id"],
            chunk_id="chunk-2",
            sequence=2,
            content_type="audio/webm",
            content=b"second",
        )
        gateway.transcription_release.set()

        await _wait_until(lambda: store.get_meeting(meeting["id"])["lifecycle"] == "review")
        transcript = service.snapshot(meeting["id"])["transcript"]
        assert [item["text"] for item in transcript] == ["최신 전체 전사"]
        assert len(gateway.transcription_calls) == 2
        assert gateway.transcription_calls[1]["content"] == b"firstsecond"
        assert gateway.minutes_calls == 1
        source = store.get_minutes(meeting["id"])["structured"]["_source"]
        assert transcript[0]["source"].startswith(
            f"diarized:{source['audio_manifest_sha256']}:"
        )
    finally:
        gateway.transcription_release.set()
        await service.stop_workers()
        store.close()


async def _wait_until(predicate, timeout: float = 3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("condition was not met before timeout")
