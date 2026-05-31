from datetime import datetime

from panel.rolling_buffer import RollingBuffer, Utterance
from panel.triggers import RealtimeTriggerEngine


def _buffer(*texts: str) -> RollingBuffer:
    buffer = RollingBuffer(maxlen=10)
    for text in texts:
        buffer.add(Utterance(timestamp=datetime.now(), speaker="t", text=text))
    return buffer


def test_fact_checker_triggers_on_numbers():
    engine = RealtimeTriggerEngine()
    events = engine.evaluate(
        "이번 분기 매출은 목표 대비 108퍼센트입니다",
        _buffer("이번 분기 매출은 목표 대비 108퍼센트입니다"),
    )

    assert any(event.panel_name == "fact_checker" for event in events)


def test_trigger_event_has_focus_keyword():
    engine = RealtimeTriggerEngine()
    events = engine.evaluate(
        "Claude Opus 4.8과 Codex 비교 벤치마크를 확인해야 합니다",
        _buffer("Claude Opus 4.8과 Codex 비교 벤치마크를 확인해야 합니다"),
    )

    assert any("Claude Opus 4.8" in event.focus_keyword for event in events)


def test_devils_advocate_triggers_on_decision_signal():
    engine = RealtimeTriggerEngine()
    events = engine.evaluate(
        "그럼 이 안으로 확정하고 진행하죠",
        _buffer("그럼 이 안으로 확정하고 진행하죠"),
    )

    assert any(event.panel_name == "devils_advocate" for event in events)


def test_new_feed_panels_trigger_on_open_action_customer_signals():
    engine = RealtimeTriggerEngine()
    events = engine.evaluate(
        "고객 피드백은 나중에 확인 필요하고 제가 내일까지 정리하겠습니다",
        _buffer("고객 피드백은 나중에 확인 필요하고 제가 내일까지 정리하겠습니다"),
    )
    names = {event.panel_name for event in events}

    assert "parking_lot" in names
    assert "action_candidate" in names
    assert "customer_perspective" in names


def test_disabled_panels_do_not_trigger():
    engine = RealtimeTriggerEngine()
    events = engine.evaluate(
        "매출은 108퍼센트이고 이 안으로 확정하죠",
        _buffer("매출은 108퍼센트이고 이 안으로 확정하죠"),
        enabled_panels=set(),
    )

    assert events == []
