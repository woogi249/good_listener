import asyncio

import pytest

from panel import realtime_app
from panel.session import MeetingSession


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeManager:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):
        self.messages.append(message)


async def _sleep_forever():
    await asyncio.sleep(60)


@pytest.mark.anyio
async def test_reset_app_state_cancels_playback_and_returns_ready_state(monkeypatch):
    fake_manager = FakeManager()
    test_session = MeetingSession(mock_ai=True, ai_provider="cli")
    test_session.start()
    test_session.set_mic_running(True)
    test_session.prepare_context(topic="데모", goal="초기화")
    test_session.set_panel_enabled("ideator", False)
    test_session.add_utterance("한은 기준금리 2.50% 확인 필요")

    sample_task = asyncio.create_task(_sleep_forever())
    demo_task = asyncio.create_task(_sleep_forever())
    mic_task = asyncio.create_task(_sleep_forever())
    panel_task = asyncio.create_task(_sleep_forever())

    monkeypatch.setattr(realtime_app, "manager", fake_manager)
    monkeypatch.setattr(realtime_app, "session", test_session)
    monkeypatch.setattr(realtime_app, "_sample_task", sample_task)
    monkeypatch.setattr(realtime_app, "_demo_task", demo_task)
    monkeypatch.setattr(realtime_app, "_mic_task", mic_task)
    monkeypatch.setattr(realtime_app, "_demo_paused", True)
    monkeypatch.setattr(realtime_app, "_demo_stop_requested", False)
    realtime_app._panel_tasks.clear()
    realtime_app._panel_tasks.add(panel_task)

    await realtime_app.reset_app_state()

    assert realtime_app._sample_task is None
    assert realtime_app._demo_task is None
    assert realtime_app._mic_task is None
    assert realtime_app._panel_tasks == set()
    assert realtime_app._demo_paused is False
    assert realtime_app._demo_stop_requested is False
    assert sample_task.cancelled()
    assert demo_task.cancelled()
    assert mic_task.cancelled()
    assert panel_task.cancelled()

    state = test_session.state()
    assert state["running"] is False
    assert state["mic_running"] is False
    assert state["ai_provider"] == "exaone"
    assert state["transcript"] == []
    assert "ideator" in state["enabled_panels"]
    assert fake_manager.messages[-2] == {"type": "demo_utterance_end", "stopped": True}
    assert fake_manager.messages[-1]["type"] == "state"


@pytest.mark.anyio
async def test_start_demo_after_reset_starts_from_clean_task_slot(monkeypatch):
    fake_manager = FakeManager()
    test_session = MeetingSession(mock_ai=True)
    started_modes = []

    async def fake_demo_loop(mode):
        started_modes.append(mode)

    monkeypatch.setattr(realtime_app, "manager", fake_manager)
    monkeypatch.setattr(realtime_app, "session", test_session)
    monkeypatch.setattr(realtime_app, "_sample_task", None)
    monkeypatch.setattr(realtime_app, "_demo_task", None)
    monkeypatch.setattr(realtime_app, "_mic_task", None)
    monkeypatch.setattr(realtime_app, "_demo_loop", fake_demo_loop)
    realtime_app._panel_tasks.clear()

    await realtime_app.reset_app_state()
    await realtime_app.start_demo_playback("fixture")
    await realtime_app._demo_task

    assert started_modes == ["fixture"]
    assert test_session.state()["running"] is True
    assert test_session.state()["ai_provider"] == "cli"
