"""실시간 4패널 회의 앱 서버."""
from __future__ import annotations

import argparse
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import DEFAULT_STT_CHUNK_S
from .session import MeetingSession
from .stt import WhisperTranscriber
from .triggers import TriggerEvent
from .vocabulary import (
    build_hotwords,
    build_session_prompt,
    load_domain_prompt,
    parse_terms,
)

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
SAMPLE_TRANSCRIPT = ROOT_DIR / "samples" / "mock_transcript.txt"

app = FastAPI(title="realtime-panel")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

session = MeetingSession()
cli_executor = ThreadPoolExecutor(max_workers=2)
stt_executor = ThreadPoolExecutor(max_workers=1)
_sample_task: asyncio.Task | None = None
_mic_task: asyncio.Task | None = None
_transcriber: WhisperTranscriber | None = None
_transcriber_lock = threading.Lock()
_domain_profile: str | None = "ai"
_base_stt_prompt: str | None = load_domain_prompt("ai")
_stt_initial_prompt: str | None = build_session_prompt(base_prompt=_base_stt_prompt)
_stt_hotwords: str | None = build_hotwords("ai")


class ConnectionManager:
    def __init__(self) -> None:
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.active.discard(websocket)

    async def broadcast(self, message: dict) -> None:
        dead: list[WebSocket] = []
        for websocket in self.active:
            try:
                await websocket.send_json(message)
            except RuntimeError:
                dead.append(websocket)
        for websocket in dead:
            self.disconnect(websocket)


manager = ConnectionManager()


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/state")
async def get_state() -> dict:
    return session.state()


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    await websocket.send_json({"type": "state", "state": session.state()})
    try:
        while True:
            message = await websocket.receive_json()
            await handle_message(message)
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def handle_message(message: dict[str, Any]) -> None:
    message_type = message.get("type")
    if message_type == "start":
        await manager.broadcast({"type": "state", "state": session.start()})
    elif message_type == "stop":
        await stop_mic()
        await manager.broadcast({"type": "state", "state": session.stop()})
    elif message_type == "reset":
        state = session.reset()
        update_stt_context()
        await manager.broadcast({"type": "state", "state": state})
    elif message_type == "prepare":
        terms = parse_terms(message.get("terms"))
        state = session.prepare_context(
            topic=str(message.get("topic", "")),
            goal=str(message.get("goal", "")),
            terms=terms,
        )
        update_stt_context()
        await manager.broadcast({"type": "state", "state": state})
    elif message_type == "utterance":
        if not session.running:
            session.start()
        await process_utterance(str(message.get("text", "")), source="manual")
    elif message_type == "toggle_panel":
        panel = str(message.get("panel", ""))
        enabled = bool(message.get("enabled"))
        await manager.broadcast(
            {"type": "state", "state": session.set_panel_enabled(panel, enabled)}
        )
    elif message_type == "play_sample":
        await start_sample_playback()
    elif message_type == "start_mic":
        await start_mic()
    elif message_type == "stop_mic":
        await stop_mic()


async def process_utterance(text: str, source: str) -> None:
    item, events = session.add_utterance(text=text, speaker=source)
    if item is None:
        return
    await manager.broadcast({"type": "transcript", "item": item.as_dict()})
    await manager.broadcast({"type": "state", "state": session.state()})
    for event in events:
        asyncio.create_task(run_panel_event(event))


async def run_panel_event(event: TriggerEvent) -> None:
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(cli_executor, session.run_panel, event)
    await manager.broadcast({"type": "panel_update", "panel": result.as_dict()})
    await manager.broadcast({"type": "state", "state": session.state()})


async def start_sample_playback() -> None:
    global _sample_task
    if _sample_task and not _sample_task.done():
        return
    session.start()
    await manager.broadcast({"type": "state", "state": session.state()})
    _sample_task = asyncio.create_task(_sample_loop())


async def _sample_loop(delay_s: float = 1.5) -> None:
    if not SAMPLE_TRANSCRIPT.exists():
        await manager.broadcast({"type": "status", "message": "sample transcript not found"})
        return
    lines = [
        line.strip()
        for line in SAMPLE_TRANSCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for line in lines:
        if not session.running:
            break
        await process_utterance(line, source="sample")
        await asyncio.sleep(delay_s)


async def start_mic() -> None:
    global _mic_task
    if _mic_task and not _mic_task.done():
        return
    session.set_mic_running(True)
    await manager.broadcast({"type": "state", "state": session.state()})
    _mic_task = asyncio.create_task(_mic_loop())


async def stop_mic() -> None:
    session.set_mic_running(False)
    await manager.broadcast({"type": "state", "state": session.state()})


def _record_chunk(duration_s: int) -> Any:
    import sounddevice as sd

    sample_rate = 16000
    audio = sd.rec(
        int(duration_s * sample_rate),
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()
    return audio.reshape(-1)


def _transcribe_chunk(audio: Any) -> list[str]:
    global _transcriber
    with _transcriber_lock:
        if _transcriber is None:
            _transcriber = WhisperTranscriber(model_size="small")
    result = _transcriber.transcribe_audio(
        audio,
        sample_rate=16000,
        initial_prompt=_stt_initial_prompt,
        hotwords=_stt_hotwords,
        correction_profile=_domain_profile,
    )
    return [segment.text for segment in result.segments]


def update_stt_context() -> None:
    global _stt_initial_prompt, _stt_hotwords
    context = session.state().get("context", {})
    _stt_initial_prompt = build_session_prompt(
        topic=context.get("topic", ""),
        goal=context.get("goal", ""),
        terms=context.get("terms", []),
        base_prompt=_base_stt_prompt,
    )
    _stt_hotwords = build_hotwords(_domain_profile, context.get("terms", []))


async def _mic_loop() -> None:
    loop = asyncio.get_running_loop()
    await manager.broadcast({"type": "status", "message": "mic listening"})
    while session.mic_running:
        try:
            audio = await loop.run_in_executor(
                stt_executor,
                _record_chunk,
                DEFAULT_STT_CHUNK_S,
            )
            texts = await loop.run_in_executor(stt_executor, _transcribe_chunk, audio)
        except Exception as exc:
            session.set_mic_running(False)
            await manager.broadcast({"type": "status", "message": f"mic error: {exc}"})
            await manager.broadcast({"type": "state", "state": session.state()})
            return
        for text in texts:
            await process_utterance(text, source="mic")


def main() -> None:
    parser = argparse.ArgumentParser(description="realtime-panel 4-panel app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mock-ai", action="store_true", help="CLI 호출 없이 휴리스틱 출력 사용")
    parser.add_argument(
        "--domain-profile",
        default="ai",
        choices=["ai", "none"],
        help="STT 도메인 용어집 프로필",
    )
    parser.add_argument(
        "--initial-prompt-file",
        type=Path,
        default=None,
        help="Whisper initial_prompt 파일 경로. 지정하면 domain profile 기본 문구를 대체",
    )
    args = parser.parse_args()

    global _domain_profile, _base_stt_prompt, _stt_initial_prompt, _stt_hotwords
    _domain_profile = None if args.domain_profile == "none" else args.domain_profile
    _base_stt_prompt = load_domain_prompt(_domain_profile, args.initial_prompt_file)
    _stt_initial_prompt = build_session_prompt(base_prompt=_base_stt_prompt)
    _stt_hotwords = build_hotwords(_domain_profile)
    session.mock_ai = args.mock_ai

    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
