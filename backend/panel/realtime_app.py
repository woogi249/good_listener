"""OpenAI-only, meeting-scoped FastAPI application."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .crypto import EncryptionManager
from .meeting_service import MeetingService, render_minutes_markdown
from .openai_gateway import OpenAIGateway, OpenAIUnavailable
from .schemas import MeetingCreate, MinutesUpdate, TranscriptFinal
from .storage import SQLiteStore, utc_now


STATIC_DIR = Path(__file__).resolve().parent / "static"
CONTROL_COOKIE = "gl_control"


@dataclass(frozen=True)
class AppSettings:
    db_path: Path
    audio_dir: Path
    key_path: Path
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "AppSettings":
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            data_dir = Path(local_app_data) / "GoodListener" / "data"
        else:
            data_dir = Path(os.getenv("XDG_DATA_HOME", "").strip() or Path.home() / ".local" / "share")
            data_dir = data_dir / "GoodListener"
        db_path = Path(os.getenv("GOOD_LISTENER_DB_PATH", "").strip() or data_dir / "good-listener.db")
        audio_dir = Path(os.getenv("GOOD_LISTENER_AUDIO_DIR", "").strip() or data_dir / "audio")
        key_default = data_dir / ("master.key.dpapi" if os.name == "nt" else "master.key")
        key_path = Path(os.getenv("GOOD_LISTENER_KEY_PATH", "").strip() or key_default)
        origins = tuple(
            value.strip().rstrip("/")
            for value in os.getenv("GOOD_LISTENER_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        )
        return cls(db_path=db_path, audio_dir=audio_dir, key_path=key_path, allowed_origins=origins)


class MeetingSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, meeting_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.setdefault(meeting_id, set()).add(websocket)

    async def disconnect(self, meeting_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            connections = self._connections.get(meeting_id)
            if connections is None:
                return
            connections.discard(websocket)
            if not connections:
                self._connections.pop(meeting_id, None)

    async def broadcast(self, event: dict) -> None:
        meeting_id = str(event.get("meeting_id") or "")
        async with self._lock:
            targets = list(self._connections.get(meeting_id, set()))
        dead = []
        for websocket in targets:
            try:
                await websocket.send_json(event)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            await self.disconnect(meeting_id, websocket)

    async def close_meeting(self, meeting_id: str) -> None:
        async with self._lock:
            targets = list(self._connections.pop(meeting_id, set()))
        for websocket in targets:
            try:
                await websocket.close(code=1000)
            except Exception:
                pass


def create_app(
    *,
    settings: AppSettings | None = None,
    store: SQLiteStore | None = None,
    gateway: OpenAIGateway | None = None,
    control_token: str | None = None,
) -> FastAPI:
    config = settings or AppSettings.from_env()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.audio_dir.mkdir(parents=True, exist_ok=True)
    owned_store = store is None
    cipher = EncryptionManager.load(config.key_path) if store is None else store.cipher
    database = store or SQLiteStore(config.db_path, cipher=cipher)
    openai_gateway = gateway or OpenAIGateway()
    sockets = MeetingSocketManager()
    service = MeetingService(
        store=database,
        gateway=openai_gateway,
        audio_dir=config.audio_dir,
        event_sink=sockets.broadcast,
    )
    token = control_token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        await service.start_workers()
        try:
            yield
        finally:
            await service.stop_workers()
            if owned_store:
                database.close()

    application = FastAPI(title="Good Listener", version="1.0.0", lifespan=lifespan)
    application.state.settings = config
    application.state.store = database
    application.state.gateway = openai_gateway
    application.state.meeting_service = service
    application.state.socket_manager = sockets
    application.state.control_token = token
    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @application.middleware("http")
    async def local_host_origin_guard(request: Request, call_next):
        if not _request_is_local(request, config):
            return Response(status_code=status.HTTP_403_FORBIDDEN, content="forbidden host or origin")
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store" if request.url.path.startswith("/api/") else "no-cache")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; media-src 'self' blob:; "
            "connect-src 'self' ws://127.0.0.1:* ws://localhost:* ws://[::1]:* "
            "https://api.openai.com; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        response.headers.setdefault("Permissions-Policy", "microphone=(self)")
        return response

    def require_control(gl_control: str | None = Cookie(default=None)) -> None:
        if gl_control is None or not hmac.compare_digest(gl_control, token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="local control authorization required")

    @application.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @application.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "storage": "ok",
            "encryption": "aes-256-gcm",
            "openai_configured": openai_gateway.configured,
            "workers_running": bool(service._tasks),
        }

    @application.get("/api/bootstrap")
    async def bootstrap(response: Response) -> dict:
        response.set_cookie(
            CONTROL_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/",
        )
        return {
            "ready": openai_gateway.configured,
            "openai_configured": openai_gateway.configured,
            "control_authorized": True,
            "external_processing": {
                "provider": "OpenAI API",
                "consent_required": True,
                "audio_and_transcript_sent_externally": True,
            },
        }

    @application.get("/api/preflight")
    async def preflight() -> dict:
        return {
            "ready": openai_gateway.configured,
            "message": "Ready" if openai_gateway.configured else "OPENAI_API_KEY missing",
            "checks": {
                "storage": True,
                "encryption": True,
                "openai_api_key": openai_gateway.configured,
            },
            "external_processing": {
                "provider": "OpenAI API",
                "consent_required": True,
                "audio_and_transcript_sent_externally": True,
            },
        }

    @application.get("/api/meetings")
    async def list_meetings(_: None = Depends(require_control)) -> list[dict]:
        return database.list_meetings()

    @application.post("/api/meetings", status_code=status.HTTP_201_CREATED)
    async def create_meeting(
        body: MeetingCreate,
        _: None = Depends(require_control),
    ) -> dict:
        if not body.consent_external_processing:
            raise HTTPException(
                status_code=422,
                detail="external processing consent is required for OpenAI-only operation",
            )
        try:
            return service.create_meeting(
                topic=body.topic,
                goal=body.goal,
                terms=body.terms,
                consent_external_processing=body.consent_external_processing,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get("/api/meetings/{meeting_id}")
    async def get_meeting(meeting_id: str, _: None = Depends(require_control)) -> dict:
        return _meeting_or_404(service, meeting_id)

    @application.post("/api/meetings/{meeting_id}/start")
    async def start_meeting(meeting_id: str, _: None = Depends(require_control)) -> dict:
        _ensure_consent(database, meeting_id)
        if not openai_gateway.configured:
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY is not configured")
        return await _lifecycle_call(service.start_meeting, meeting_id)

    @application.post("/api/meetings/{meeting_id}/pause")
    async def pause_meeting(meeting_id: str, _: None = Depends(require_control)) -> dict:
        return await _lifecycle_call(service.pause_meeting, meeting_id)

    @application.post("/api/meetings/{meeting_id}/resume")
    async def resume_meeting(meeting_id: str, _: None = Depends(require_control)) -> dict:
        return await _lifecycle_call(service.resume_meeting, meeting_id)

    @application.post("/api/meetings/{meeting_id}/stop", status_code=status.HTTP_202_ACCEPTED)
    async def stop_meeting(meeting_id: str, _: None = Depends(require_control)) -> dict:
        return await _lifecycle_call(service.stop_meeting, meeting_id)

    @application.post("/api/meetings/{meeting_id}/retry-finalization", status_code=status.HTTP_202_ACCEPTED)
    async def retry_finalization(meeting_id: str, _: None = Depends(require_control)) -> dict:
        return await _lifecycle_call(service.retry_finalization, meeting_id)

    @application.post("/api/meetings/{meeting_id}/realtime/client-secret")
    async def realtime_client_secret(
        meeting_id: str,
        _: None = Depends(require_control),
    ) -> dict:
        meeting = _ensure_consent(database, meeting_id)
        if meeting["lifecycle"] not in {"live", "paused"}:
            raise HTTPException(status_code=409, detail="meeting must be live before creating a Realtime session")
        try:
            secret = await openai_gateway.create_realtime_client_secret(
                topic=meeting["topic"],
                goal=meeting["goal"],
                terms=meeting["terms"],
            )
        except OpenAIUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"meeting_id": meeting_id, **secret}

    @application.get("/api/meetings/{meeting_id}/transcript")
    async def transcript(meeting_id: str, _: None = Depends(require_control)) -> dict:
        snapshot = _meeting_or_404(service, meeting_id)
        return {
            "meeting_id": meeting_id,
            "transcript_revision": snapshot["transcript_revision"],
            "items": snapshot["transcript"],
        }

    @application.post("/api/meetings/{meeting_id}/audio/chunks")
    async def upload_audio_chunk(
        meeting_id: str,
        file: UploadFile = File(...),
        chunk_id: str = Form(...),
        sequence: int = Form(...),
        content_type: str = Form(default=""),
        sha256: str = Form(default=""),
        started_at: str | None = Form(default=None),
        ended_at: str | None = Form(default=None),
        _: None = Depends(require_control),
    ) -> dict:
        content = await file.read(32 * 1024 * 1024 + 1)
        try:
            metadata, inserted = await service.persist_audio_chunk(
                meeting_id,
                chunk_id=chunk_id,
                sequence=sequence,
                content_type=content_type or file.content_type or "application/octet-stream",
                content=content,
                expected_sha256=sha256,
                started_at=started_at,
                ended_at=ended_at,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="meeting not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "meeting_id": meeting_id,
            "chunk_id": metadata["chunk_id"],
            "sequence": metadata["sequence"],
            "persisted": True,
            "duplicate": not inserted,
            "lifecycle": metadata["lifecycle"],
            "minutes_stale": metadata["minutes_stale"],
            "regeneration_started": metadata["regeneration_started"],
        }

    @application.get("/api/meetings/{meeting_id}/minutes")
    async def get_minutes(meeting_id: str, _: None = Depends(require_control)) -> dict:
        try:
            minutes = database.get_minutes(meeting_id)
            database.get_meeting(meeting_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="meeting not found") from exc
        if minutes is None:
            raise HTTPException(status_code=404, detail="minutes not ready")
        return minutes

    @application.patch("/api/meetings/{meeting_id}/minutes")
    async def update_minutes(
        meeting_id: str,
        body: MinutesUpdate,
        _: None = Depends(require_control),
    ) -> dict:
        try:
            meeting = database.get_meeting(meeting_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="meeting not found") from exc
        if meeting["lifecycle"] != "review" or database.get_minutes(meeting_id) is None:
            raise HTTPException(status_code=409, detail="minutes are not ready for review")
        markdown = body.markdown if body.markdown is not None else render_minutes_markdown(body.structured)
        minutes, event = database.upsert_minutes(
            meeting_id,
            transcript_revision=meeting["transcript_revision"],
            structured=body.structured,
            markdown=markdown,
            status="draft",
        )
        await sockets.broadcast(event)
        return minutes

    @application.post("/api/meetings/{meeting_id}/minutes/approve")
    async def approve_minutes(meeting_id: str, _: None = Depends(require_control)) -> dict:
        try:
            return await service.approve_minutes(meeting_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="minutes not ready") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.delete("/api/meetings/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_meeting(meeting_id: str, _: None = Depends(require_control)) -> Response:
        try:
            meeting = database.get_meeting(meeting_id)
            deleting = _ephemeral_event(
                meeting_id,
                "meeting.deleted",
                meeting["revision"] + 1,
                {"permanent": True},
            )
            await sockets.broadcast(deleting)
            await service.delete_meeting(meeting_id)
            await sockets.close_meeting(meeting_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="meeting not found") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.websocket("/ws/meetings/{meeting_id}")
    async def meeting_websocket(websocket: WebSocket, meeting_id: str) -> None:
        if not _websocket_is_local(websocket, config):
            await websocket.close(code=4403)
            return
        if not hmac.compare_digest(websocket.cookies.get(CONTROL_COOKIE, ""), token):
            # Closing before accept becomes an HTTP 403 handshake denial, so the
            # browser cannot observe the application close code and refresh its
            # stale control cookie.  Same-origin unauthenticated sockets receive
            # no application data; they are accepted only to deliver code 4403.
            await websocket.accept()
            await websocket.close(code=4403)
            return
        try:
            database.get_meeting(meeting_id)
        except KeyError:
            await websocket.close(code=4404)
            return
        await sockets.connect(meeting_id, websocket)
        try:
            after_revision = max(0, int(websocket.query_params.get("after_revision", "0")))
        except ValueError:
            after_revision = 0
        try:
            for event in database.events_after(meeting_id, after_revision):
                await websocket.send_json(event)
            snapshot = service.snapshot(meeting_id)
            await websocket.send_json(
                _ephemeral_event(
                    meeting_id,
                    "meeting.snapshot",
                    snapshot["revision"],
                    snapshot,
                )
            )
            while True:
                message = await websocket.receive_json()
                await _handle_socket_message(
                    message=message,
                    meeting_id=meeting_id,
                    websocket=websocket,
                    service=service,
                    store=database,
                    sockets=sockets,
                )
        except WebSocketDisconnect:
            pass
        finally:
            await sockets.disconnect(meeting_id, websocket)

    return application


async def _handle_socket_message(
    *,
    message: dict[str, Any],
    meeting_id: str,
    websocket: WebSocket,
    service: MeetingService,
    store: SQLiteStore,
    sockets: MeetingSocketManager,
) -> None:
    message_type = str(message.get("type") or "")
    if message_type == "transcript.partial":
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else message
        text = str(payload.get("text") or payload.get("delta") or "")[:20_000]
        event = _ephemeral_event(
            meeting_id,
            "transcript.partial",
            store.get_meeting(meeting_id)["revision"],
            {"item_id": str(payload.get("item_id") or "")[:200], "text": text},
        )
        await sockets.broadcast(event)
        return
    if message_type == "transcript.final":
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else message
        try:
            data = TranscriptFinal.model_validate(payload)
            utterance, inserted = await service.ingest_final(meeting_id, data)
        except ValueError as exc:
            await websocket.send_json(
                _ephemeral_event(meeting_id, "error", store.get_meeting(meeting_id)["revision"], {"code": "invalid_transcript", "message": str(exc)[:300]})
            )
            return
        await websocket.send_json(
            _ephemeral_event(
                meeting_id,
                "transcript.ack",
                store.get_meeting(meeting_id)["revision"],
                {"item_id": data.item_id, "utterance_id": utterance["id"], "duplicate": not inserted},
            )
        )
        return
    if message_type == "realtime.status":
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        safe = {
            "status": str(payload.get("status") or "unknown")[:50],
            "session_id": str(payload.get("session_id") or "")[:200],
            "error_code": str(payload.get("error_code") or "")[:100],
        }
        event = store.append_event(meeting_id, "realtime.status", safe)
        await sockets.broadcast(event)
        return
    if message_type == "audio.metadata":
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        event = _ephemeral_event(
            meeting_id,
            "audio.metadata.ack",
            store.get_meeting(meeting_id)["revision"],
            {
                "chunk_id": str(payload.get("chunk_id") or "")[:200],
                "sequence": payload.get("sequence"),
            },
        )
        await websocket.send_json(event)
        return
    if message_type == "ping":
        await websocket.send_json(
            _ephemeral_event(meeting_id, "pong", store.get_meeting(meeting_id)["revision"], {})
        )


def _meeting_or_404(service: MeetingService, meeting_id: str) -> dict:
    try:
        return service.snapshot(meeting_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="meeting not found") from exc


def _ensure_consent(store: SQLiteStore, meeting_id: str) -> dict:
    try:
        meeting = store.get_meeting(meeting_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="meeting not found") from exc
    if not meeting["consent_external_processing"]:
        raise HTTPException(status_code=409, detail="external processing consent is required")
    return meeting


async def _lifecycle_call(call, meeting_id: str) -> dict:
    try:
        return await call(meeting_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="meeting not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _request_is_local(request: Request, settings: AppSettings) -> bool:
    host = request.url.hostname or ""
    if host not in settings.allowed_hosts:
        return False
    origin = request.headers.get("origin")
    return _origin_allowed(
        origin,
        settings,
        expected_host=request.headers.get("host", ""),
        expected_scheme=request.url.scheme,
    )


def _websocket_is_local(websocket: WebSocket, settings: AppSettings) -> bool:
    host_header = websocket.headers.get("host", "")
    host = _host_without_port(host_header)
    if host not in settings.allowed_hosts:
        return False
    return _origin_allowed(
        websocket.headers.get("origin"),
        settings,
        expected_host=host_header,
        expected_scheme=websocket.url.scheme,
    )


def _origin_allowed(
    origin: str | None,
    settings: AppSettings,
    *,
    expected_host: str,
    expected_scheme: str,
) -> bool:
    if not origin:
        return True
    normalized = origin.rstrip("/")
    if normalized in settings.allowed_origins:
        return True
    parsed = urlparse(normalized)
    expected = urlparse(f"//{expected_host}")
    request_scheme = "https" if expected_scheme in {"https", "wss"} else "http"
    try:
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        expected_port = expected.port or (443 if request_scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == request_scheme
        and (parsed.hostname or "") == (expected.hostname or "")
        and parsed.hostname in settings.allowed_hosts
        and origin_port == expected_port
    )


def _host_without_port(value: str) -> str:
    if value.startswith("["):
        return value.partition("]")[0].lstrip("[")
    return value.split(":", 1)[0]


def _ephemeral_event(meeting_id: str, event_type: str, revision: int, payload: dict) -> dict:
    return {
        "type": event_type,
        "meeting_id": meeting_id,
        "event_id": str(uuid.uuid4()),
        "seq": 0,
        "revision": int(revision),
        "occurred_at": utc_now(),
        "payload": payload,
    }


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Good Listener OpenAI realtime meeting app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("Good Listener binds to loopback only; use a trusted TLS reverse proxy for remote access")
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
