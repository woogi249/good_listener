"""실시간 4패널 회의 앱 서버."""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import DEFAULT_STT_CHUNK_S
from .session import InsightFeedItem, MeetingSession
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
DEMO_TIMELINE = ROOT_DIR / "samples" / "demo_meeting.timeline.json"

app = FastAPI(title="realtime-panel")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

session = MeetingSession()
cli_executor = ThreadPoolExecutor(max_workers=5)
stt_executor = ThreadPoolExecutor(max_workers=1)
_sample_task: asyncio.Task | None = None
_demo_task: asyncio.Task | None = None
_demo_paused = False
_demo_stop_requested = False
_mic_task: asyncio.Task | None = None
_panel_tasks: set[asyncio.Task] = set()
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
        await reset_app_state()
    elif message_type == "prepare":
        terms = parse_terms(message.get("terms"))
        state = session.prepare_context(
            topic=str(message.get("topic", "")),
            goal=str(message.get("goal", "")),
            terms=terms,
        )
        update_stt_context()
        await manager.broadcast({"type": "state", "state": state})
    elif message_type == "set_provider":
        await manager.broadcast(
            {"type": "state", "state": session.set_ai_provider(str(message.get("provider", "cli")))}
        )
    elif message_type == "utterance":
        if not session.running:
            session.start()
        provider = message.get("provider")
        if provider:
            session.set_ai_provider(str(provider))
        await process_utterance(str(message.get("text", "")), source="manual")
    elif message_type == "toggle_panel":
        panel = str(message.get("panel", ""))
        enabled = bool(message.get("enabled"))
        await manager.broadcast(
            {"type": "state", "state": session.set_panel_enabled(panel, enabled)}
        )
    elif message_type == "toggle_layout_arbiter":
        await manager.broadcast(
            {
                "type": "state",
                "state": session.set_layout_arbiter_enabled(bool(message.get("enabled"))),
            }
        )
    elif message_type == "play_sample":
        await start_sample_playback()
    elif message_type == "play_demo_script":
        await start_demo_playback(str(message.get("mode", "fixture")))
    elif message_type == "pause_demo_script":
        await pause_demo_playback()
    elif message_type == "resume_demo_script":
        await resume_demo_playback()
    elif message_type == "stop_demo_script":
        await stop_demo_playback()
    elif message_type == "start_mic":
        await start_mic()
    elif message_type == "stop_mic":
        await stop_mic()


async def process_utterance(
    text: str,
    source: str,
    fixture_outputs: dict | None = None,
    fixture_fallback: bool = False,
) -> None:
    item, events = session.add_utterance(
        text=text,
        speaker=source,
        fixture_outputs=fixture_outputs,
        fixture_fallback=fixture_fallback,
    )
    if item is None:
        return
    await manager.broadcast({"type": "transcript", "item": item.as_dict()})
    await manager.broadcast({"type": "state", "state": session.state()})
    for event in events:
        task = asyncio.create_task(run_panel_event(event))
        _panel_tasks.add(task)
        task.add_done_callback(_panel_tasks.discard)


async def run_panel_event(event: TriggerEvent) -> None:
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(cli_executor, session.run_panel, event)
    if isinstance(result, InsightFeedItem):
        await manager.broadcast({"type": "feed_update", "item": result.as_dict()})
    else:
        await manager.broadcast({"type": "panel_update", "panel": result.as_dict()})
    await manager.broadcast({"type": "state", "state": session.state()})


async def _cancel_task(task: asyncio.Task | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _cancel_panel_tasks() -> None:
    tasks = list(_panel_tasks)
    _panel_tasks.clear()
    for task in tasks:
        await _cancel_task(task)


async def reset_app_state() -> None:
    global _sample_task, _demo_task, _demo_paused, _demo_stop_requested, _mic_task
    _demo_stop_requested = True
    _demo_paused = False
    session.set_mic_running(False)
    await _cancel_task(_sample_task)
    await _cancel_task(_demo_task)
    await _cancel_task(_mic_task)
    await _cancel_panel_tasks()
    _sample_task = None
    _demo_task = None
    _mic_task = None
    _demo_stop_requested = False
    state = session.reset()
    update_stt_context()
    await manager.broadcast({"type": "demo_utterance_end", "stopped": True})
    await manager.broadcast({"type": "state", "state": state})


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


async def start_demo_playback(mode: str = "fixture") -> None:
    global _demo_task, _demo_paused, _demo_stop_requested
    if _demo_task and not _demo_task.done():
        return
    mode = mode if mode in {"fixture", "cli", "exaone", "friendli"} else "fixture"
    mode = "exaone" if mode == "friendli" else mode
    _demo_paused = False
    _demo_stop_requested = False
    await _cancel_panel_tasks()
    session.reset()
    session.set_ai_provider("exaone" if mode == "exaone" else "cli")
    session.start()
    await manager.broadcast({"type": "state", "state": session.state()})
    _demo_task = asyncio.create_task(_demo_loop(mode=mode))


async def pause_demo_playback() -> None:
    global _demo_paused
    _demo_paused = True
    await manager.broadcast({"type": "status", "message": "demo paused"})


async def resume_demo_playback() -> None:
    global _demo_paused
    _demo_paused = False
    await manager.broadcast({"type": "status", "message": "demo running"})


async def stop_demo_playback() -> None:
    global _demo_stop_requested, _demo_paused
    _demo_stop_requested = True
    _demo_paused = False
    await manager.broadcast({"type": "status", "message": "demo stopped"})


async def _sleep_demo(duration_s: float) -> None:
    remaining = max(0.0, duration_s)
    while remaining > 0 and not _demo_stop_requested:
        if _demo_paused:
            await asyncio.sleep(0.1)
            continue
        step = min(0.1, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _demo_loop(mode: str) -> None:
    if not DEMO_TIMELINE.exists():
        await manager.broadcast({"type": "status", "message": "demo timeline not found"})
        return
    try:
        timeline = json.loads(DEMO_TIMELINE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        await manager.broadcast({"type": "status", "message": f"demo timeline invalid: {exc}"})
        return

    try:
        playback_speed = float(timeline.get("playback_speed") or 1.0)
    except (TypeError, ValueError):
        playback_speed = 1.0
    playback_speed = max(0.1, playback_speed)
    typing_ms = max(16, round(int(timeline.get("typing_ms") or 32) / playback_speed))
    utterances = timeline.get("utterances") or []
    await manager.broadcast({"type": "status", "message": f"demo running ({mode}, {playback_speed:.1f}x)"})

    for index, utterance in enumerate(utterances, start=1):
        if _demo_stop_requested or not session.running:
            break
        await _sleep_demo(float(utterance.get("at_s") or 0.8) / playback_speed)
        if _demo_stop_requested or not session.running:
            break

        speaker = str(utterance.get("speaker") or "demo")
        text = str(utterance.get("text") or "").strip()
        if not text:
            continue

        await manager.broadcast({
            "type": "demo_utterance_start",
            "item": {
                "index": index,
                "id": utterance.get("id") or f"demo-{index}",
                "speaker": speaker,
                "text": text,
                "typing_ms": typing_ms,
            },
        })
        await _sleep_demo((len(text) * typing_ms / 1000) + (0.35 / playback_speed))
        if _demo_stop_requested or not session.running:
            break

        await process_utterance(
            text,
            source=speaker,
            fixture_outputs=utterance.get("fixture_outputs") if mode == "fixture" else None,
            fixture_fallback=mode == "fixture",
        )

    await manager.broadcast({
        "type": "demo_utterance_end",
        "stopped": _demo_stop_requested,
    })
    if not _demo_stop_requested:
        await manager.broadcast({"type": "status", "message": "demo complete"})


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
        "--provider",
        default="exaone",
        help="실제 분석 provider. fixture/mock이 아닐 때 사용 (exaone 또는 cli)",
    )
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
    session.set_ai_provider(args.provider)

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
