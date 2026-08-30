from pathlib import Path

import pytest

from panel.crypto import EncryptionManager
from panel.storage import SQLiteStore


@pytest.fixture
def store(tmp_path: Path):
    value = SQLiteStore(tmp_path / "meetings.db", cipher=EncryptionManager(b"k" * 32))
    yield value
    value.close()


def test_sensitive_database_content_is_encrypted_and_events_are_monotonic(store):
    meeting = store.create_meeting(
        topic="비밀 제품 회의",
        goal="출시 결정",
        terms=["프로젝트 은하"],
        consent_external_processing=True,
    )
    meeting, lifecycle_event = store.transition(meeting["id"], "live")
    utterance, transcript_event, inserted = store.add_utterance(
        meeting["id"], external_item_id="openai-item-1", text="비밀 매출은 10억원입니다"
    )

    assert inserted is True
    assert utterance["text"] == "비밀 매출은 10억원입니다"
    assert [event["revision"] for event in store.events_after(meeting["id"])] == [1, 2, 3]
    assert lifecycle_event["meeting_id"] == meeting["id"]
    assert transcript_event["seq"] == 3

    raw_topic = store._conn.execute("SELECT topic FROM meetings WHERE id=?", (meeting["id"],)).fetchone()[0]
    raw_text = store._conn.execute("SELECT text FROM utterances WHERE id=?", (utterance["id"],)).fetchone()[0]
    raw_event = store._conn.execute(
        "SELECT payload_json FROM events WHERE event_id=?", (transcript_event["event_id"],)
    ).fetchone()[0]
    assert raw_topic.startswith("enc:v1:")
    assert raw_text.startswith("enc:v1:")
    assert raw_event.startswith("enc:v1:")
    assert "비밀" not in raw_topic + raw_text + raw_event


def test_user_text_that_looks_like_ciphertext_round_trips_safely(store):
    topic = "enc:v1:c2VjcmV0"

    meeting = store.create_meeting(topic=topic, consent_external_processing=True)
    raw_topic = store._conn.execute(
        "SELECT topic FROM meetings WHERE id=?", (meeting["id"],)
    ).fetchone()[0]

    assert meeting["topic"] == topic
    assert raw_topic != topic
    assert store.get_meeting(meeting["id"])["topic"] == topic


def test_utterance_and_finalize_job_are_idempotent(store):
    meeting = store.create_meeting(consent_external_processing=True)
    store.transition(meeting["id"], "live")
    first, _, inserted = store.add_utterance(
        meeting["id"], external_item_id="same", text="한 번만 저장"
    )
    second, event, duplicate_inserted = store.add_utterance(
        meeting["id"], external_item_id="same", text="다른 재전송"
    )
    current = store.get_meeting(meeting["id"])
    job1, created1 = store.enqueue_job(
        meeting["id"],
        kind="finalize",
        dedupe_key="transcript:1",
        base_revision=current["revision"],
        payload={},
    )
    job2, created2 = store.enqueue_job(
        meeting["id"],
        kind="finalize",
        dedupe_key="transcript:1",
        base_revision=current["revision"],
        payload={},
    )

    assert inserted is True
    assert duplicate_inserted is False
    assert event is None
    assert first["id"] == second["id"]
    assert store.get_meeting(meeting["id"])["transcript_revision"] == 1
    assert created1 is True and created2 is False
    assert job1["id"] == job2["id"]


def test_only_one_meeting_can_be_active_while_review_history_remains_available(store):
    first = store.create_meeting(topic="첫 회의", consent_external_processing=True)
    second = store.create_meeting(topic="둘째 회의", consent_external_processing=True)
    store.transition(first["id"], "live")

    with pytest.raises(ValueError, match="already active"):
        store.transition(second["id"], "live")
    with pytest.raises(ValueError, match="already active"):
        store.create_meeting(topic="셋째 회의", consent_external_processing=True)

    store.transition(first["id"], "finalizing")
    with pytest.raises(ValueError, match="already active"):
        store.transition(second["id"], "live")

    store.transition(first["id"], "review")
    started, _ = store.transition(second["id"], "live")
    assert started["lifecycle"] == "live"
    assert store.get_meeting(first["id"])["lifecycle"] == "review"


def test_job_lease_retry_and_stale_snapshot_guard(store):
    meeting = store.create_meeting(consent_external_processing=True)
    job, _ = store.enqueue_job(
        meeting["id"], kind="fact", dedupe_key="claim", base_revision=1, payload={"secret": "x"}
    )
    claimed = store.claim_job(["fact"], lease_seconds=0)
    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert store.requeue_expired_jobs() == 1
    reclaimed = store.claim_job(["fact"], lease_seconds=30)
    assert reclaimed["attempts"] == 2
    assert store.complete_job(reclaimed["id"], reclaimed["lease_id"]) is True

    newer, _, applied = store.save_snapshot_if_fresh(
        meeting["id"], base_revision=10, data={"current_topic": "최신"}
    )
    stale, event, stale_applied = store.save_snapshot_if_fresh(
        meeting["id"], base_revision=9, data={"current_topic": "과거"}
    )
    assert applied is True
    assert stale_applied is False
    assert event is None
    assert stale["id"] == newer["id"]
    assert store.latest_snapshot(meeting["id"])["data"]["current_topic"] == "최신"


def test_minutes_and_explicit_delete_cascade(store, tmp_path: Path):
    meeting = store.create_meeting(consent_external_processing=True)
    audio = tmp_path / "chunk.glenc"
    audio.write_bytes(b"encrypted")
    store.add_audio_metadata(
        meeting["id"],
        chunk_id="c1",
        sequence=1,
        content_type="audio/webm",
        size_bytes=3,
        sha256="abc",
        path=str(audio),
        started_at=None,
        ended_at=None,
    )
    minutes, _ = store.upsert_minutes(
        meeting["id"],
        transcript_revision=0,
        structured={"title": "비밀 회의록"},
        markdown="# 비밀 회의록",
    )
    assert minutes["structured"]["title"] == "비밀 회의록"
    raw = store._conn.execute(
        "SELECT structured_json,markdown FROM minutes WHERE meeting_id=?", (meeting["id"],)
    ).fetchone()
    assert raw[0].startswith("enc:v1:") and raw[1].startswith("enc:v1:")

    paths = store.delete_meeting(meeting["id"])
    assert paths == [str(audio)]
    assert store._conn.execute("SELECT COUNT(*) FROM minutes").fetchone()[0] == 0
    with pytest.raises(KeyError):
        store.get_meeting(meeting["id"])
