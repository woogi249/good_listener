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


def test_devils_advocate_triggers_on_decision_signal():
    engine = RealtimeTriggerEngine()
    events = engine.evaluate(
        "그럼 이 안으로 확정하고 진행하죠",
        _buffer("그럼 이 안으로 확정하고 진행하죠"),
    )

    assert any(event.panel_name == "devils_advocate" for event in events)


def test_disabled_panels_do_not_trigger():
    engine = RealtimeTriggerEngine()
    events = engine.evaluate(
        "매출은 108퍼센트이고 이 안으로 확정하죠",
        _buffer("매출은 108퍼센트이고 이 안으로 확정하죠"),
        enabled_panels=set(),
    )

    assert events == []
