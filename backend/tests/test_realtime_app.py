from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from panel.crypto import EncryptionManager
from panel.realtime_app import AppSettings, create_app
from panel.schemas import MinutesDraft, ProgressAnalysis
from panel.storage import SQLiteStore


class FakeGateway:
    configured = True

    async def create_realtime_client_secret(self, *, topic, goal, terms):
        return {
            "value": "short-lived-only",
            "expires_at": 123,
            "session": {
                "type": "transcription",
                "audio": {"input": {"transcription": {"model": "gpt-live-transcribe", "languages": ["ko", "en"]}}},
            },
        }

    async def analyze_progress(self, *, meeting, utterances, previous_state):
        return ProgressAnalysis()

    async def verify_public_fact(self, **kwargs):
        raise AssertionError("fact verification was not expected")

    async def generate_minutes(self, *, meeting, utterances, snapshot, claims):
        return MinutesDraft(title=meeting["topic"] or "회의록", summary="요약")

    async def transcribe_audio_chunk(self, **kwargs):
        return {"segments": []}


def _app(tmp_path: Path):
    store = SQLiteStore(tmp_path / "meetings.db", cipher=EncryptionManager(b"r" * 32))
    settings = AppSettings(
        db_path=tmp_path / "meetings.db",
        audio_dir=tmp_path / "audio",
        key_path=tmp_path / "key",
        allowed_hosts=("testserver",),
    )
    app = create_app(
        settings=settings,
        store=store,
        gateway=FakeGateway(),
        control_token="test-control-token",
    )
    return app, store


def _bootstrap(client: TestClient):
    response = client.get("/api/bootstrap")
    assert response.status_code == 200
    assert response.json()["external_processing"]["consent_required"] is True
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "https://api.openai.com" in response.headers["Content-Security-Policy"]
    assert response.headers["Permissions-Policy"] == "microphone=(self)"


def _create_live_meeting(client: TestClient) -> str:
    created = client.post(
        "/api/meetings",
        json={
            "topic": "제품 회의",
            "goal": "결정",
            "terms": ["Good Listener"],
            "consent_external_processing": True,
        },
    )
    assert created.status_code == 201
    meeting_id = created.json()["meeting"]["id"]
    started = client.post(f"/api/meetings/{meeting_id}/start")
    assert started.status_code == 200
    assert started.json()["lifecycle"] == "live"
    return meeting_id


def test_control_cookie_consent_and_realtime_secret(tmp_path: Path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        assert client.post("/api/meetings", json={}).status_code == 403
        _bootstrap(client)
        denied = client.post(
            "/api/meetings",
            json={"topic": "x", "consent_external_processing": False},
        )
        assert denied.status_code == 422

        meeting_id = _create_live_meeting(client)
        secret = client.post(f"/api/meetings/{meeting_id}/realtime/client-secret")
        assert secret.status_code == 200
        assert secret.json()["value"] == "short-lived-only"
        assert secret.json()["session"]["type"] == "transcription"
        assert "OPENAI_API_KEY" not in secret.text

        state = client.get(f"/api/meetings/{meeting_id}").json()
        assert state["external_processing"]["required"] is True
        assert state["meeting"]["consent_external_processing"] is True
    store.close()


def test_sensitive_mutations_reject_missing_cookie_and_evil_origin(tmp_path: Path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        meeting_id = _create_live_meeting(client)
        client.cookies.clear()
        assert client.post(f"/api/meetings/{meeting_id}/stop").status_code == 403
        assert client.post(f"/api/meetings/{meeting_id}/realtime/client-secret").status_code == 403
        assert client.delete(f"/api/meetings/{meeting_id}").status_code == 403
        assert client.post(
            f"/api/meetings/{meeting_id}/audio/chunks",
            files={"file": ("chunk.webm", b"audio", "audio/webm")},
            data={"chunk_id": "c1", "sequence": "1"},
        ).status_code == 403
        assert client.get("/api/bootstrap", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get(
            "/api/bootstrap", headers={"Origin": "http://testserver:9999"}
        ).status_code == 403
    store.close()


def test_same_origin_websocket_with_stale_cookie_receives_4403_close(tmp_path: Path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        meeting_id = _create_live_meeting(client)
        client.cookies.clear()

        with client.websocket_connect(
            f"/ws/meetings/{meeting_id}",
            headers={"Cookie": "gl_control=stale-control-token"},
        ) as socket:
            message = socket.receive()

        assert message == {"type": "websocket.close", "code": 4403, "reason": ""}
    store.close()


def test_untrusted_websocket_origin_remains_handshake_denied(tmp_path: Path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        meeting_id = _create_live_meeting(client)

        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                f"/ws/meetings/{meeting_id}",
                headers={"Origin": "https://evil.example"},
            ):
                pass

        assert denied.value.code == 4403
    store.close()


def test_second_active_meeting_is_rejected_with_conflict(tmp_path: Path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        first = client.post(
            "/api/meetings",
            json={"topic": "첫 회의", "consent_external_processing": True},
        ).json()["meeting"]["id"]
        second = client.post(
            "/api/meetings",
            json={"topic": "둘째 회의", "consent_external_processing": True},
        ).json()["meeting"]["id"]
        assert client.post(f"/api/meetings/{first}/start").status_code == 200

        blocked_start = client.post(f"/api/meetings/{second}/start")
        blocked_create = client.post(
            "/api/meetings",
            json={"topic": "셋째 회의", "consent_external_processing": True},
        )

        assert blocked_start.status_code == 409
        assert blocked_create.status_code == 409
        assert "already active" in blocked_start.json()["detail"]
    store.close()


def test_audio_is_encrypted_and_delete_removes_everything(tmp_path: Path):
    app, store = _app(tmp_path)
    with TestClient(app) as client:
        _bootstrap(client)
        meeting_id = _create_live_meeting(client)
        raw_audio = b"recognizable-raw-audio-content"
        uploaded = client.post(
            f"/api/meetings/{meeting_id}/audio/chunks",
            files={"file": ("chunk.webm", raw_audio, "audio/webm")},
            data={"chunk_id": "chunk-1", "sequence": "1", "content_type": "audio/webm"},
        )
        assert uploaded.status_code == 200
        assert uploaded.json()["lifecycle"] == "live"
        assert uploaded.json()["minutes_stale"] is False
        assert uploaded.json()["regeneration_started"] is False
        duplicate = client.post(
            f"/api/meetings/{meeting_id}/audio/chunks",
            files={"file": ("chunk.webm", raw_audio, "audio/webm")},
            data={"chunk_id": "chunk-1", "sequence": "1", "content_type": "audio/webm"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["duplicate"] is True
        assert duplicate.json()["lifecycle"] == "live"
        metadata = store.list_audio_metadata(meeting_id)[0]
        encrypted_path = Path(metadata["path"])
        assert encrypted_path.exists()
        assert raw_audio not in encrypted_path.read_bytes()

        deleted = client.delete(f"/api/meetings/{meeting_id}")
        assert deleted.status_code == 204
        assert not encrypted_path.exists()
        assert store._conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 0
        assert store._conn.execute("SELECT COUNT(*) FROM audio_metadata").fetchone()[0] == 0
    store.close()
