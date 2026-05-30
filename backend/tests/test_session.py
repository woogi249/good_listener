from panel.session import MeetingSession
from panel.triggers import TriggerEvent


def test_session_returns_importance_for_mock_devils_advocate():
    session = MeetingSession(mock_ai=True)
    event = TriggerEvent(
        panel_name="devils_advocate",
        reason="합의/결정 신호 감지",
        importance=3,
        priority="MED",
        utterance="그럼 이 안으로 확정하죠",
    )

    output = session.run_panel(event)

    assert output.panel_name == "devils_advocate"
    assert output.importance == 3
    assert "리스크" in output.text


def test_session_add_utterance_updates_transcript():
    session = MeetingSession(mock_ai=True)
    item, events = session.add_utterance("예산을 15퍼센트 증액하겠습니다")

    assert item is not None
    assert item.index == 1
    assert any(event.panel_name == "fact_checker" for event in events)


def test_prepare_context_is_in_state_and_prompt():
    session = MeetingSession(mock_ai=True)
    state = session.prepare_context(
        topic="AI 모델 업데이트",
        goal="기획 아이템 결정",
        terms=["Codex", "Claude Opus"],
    )

    assert state["context"]["topic"] == "AI 모델 업데이트"
    prompt = session._render_prompt("summarizer")
    assert "회의 준비 문맥" in prompt
    assert "Codex" in prompt
