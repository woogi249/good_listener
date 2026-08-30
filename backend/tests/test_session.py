import json

from panel.session import MeetingSession
from panel.triggers import TriggerEvent
from panel.cli_dispatcher import ClaudeResponse


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


def test_feed_event_uses_fixture_output():
    session = MeetingSession(mock_ai=False)
    item, events = session.add_utterance(
        "제가 오늘 중으로 병렬 실행 테스트를 정리하겠습니다",
        fixture_outputs={
            "action_candidate": {
                "text": "TODO: 오늘 중 병렬 실행 테스트 정리",
                "delay_s": 0.0,
                "detail": {
                    "title": "병렬 실행 점검",
                    "body": "데모 전에 병렬 실행 테스트 결과를 확인합니다.",
                    "points": ["성공/실패 케이스 분리", "지연 시간 기록"],
                    "action": "오늘 중 테스트 표 업데이트",
                },
            }
        },
    )

    assert item is not None
    event = next(event for event in events if event.panel_name == "action_candidate")
    output = session.run_panel(event)

    assert output.panel_name == "action_candidate"
    assert output.text == "TODO: 오늘 중 병렬 실행 테스트 정리"
    assert output.card_variant == "task"
    assert output.urgency >= 2
    assert output.detail_title == "병렬 실행 점검"
    assert output.detail_body == "데모 전에 병렬 실행 테스트 결과를 확인합니다."
    assert output.detail_points == ["성공/실패 케이스 분리", "지연 시간 기록"]
    assert output.detail_action == "오늘 중 테스트 표 업데이트"
    feed_item = session.state()["feed"][0]
    assert feed_item["status"] == "idle"
    assert feed_item["detail_title"] == "병렬 실행 점검"


def test_thinking_text_uses_focus_keyword_before_result():
    session = MeetingSession(mock_ai=False)
    item, events = session.add_utterance(
        "Opus 4.8 출시일과 벤치마크를 확인해야 합니다",
        fixture_outputs={
            "fact_checker": {
                "text": "확인 필요: Opus 4.8 출시일과 벤치마크",
                "delay_s": 0.0,
            }
        },
    )

    assert item is not None
    fact_event = next(event for event in events if event.panel_name == "fact_checker")
    state = session.state()
    assert "Opus 4.8" in state["panels"]["fact_checker"]["text"]
    assert state["panels"]["fact_checker"]["text"].endswith("분석중...")

    output = session.run_panel(fact_event)

    assert output.text == "확인 필요: Opus 4.8 출시일과 벤치마크"


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


def test_session_uses_exaone_provider(monkeypatch):
    called = {}

    def fake_web_fact_check(prompt, timeout_s, budget_gate=None):
        called["panel_name"] = "fact_checker"
        called["prompt"] = prompt
        called["timeout_s"] = timeout_s
        return ClaudeResponse(
            success=True,
            stdout="근거부족: Opus 4.8 벤치마크 출처 불충분",
            stderr="",
            elapsed_s=0.2,
            provider="exaone+web",
            sources=[{"title": "Anthropic", "url": "https://example.com", "snippet": ""}],
        )

    monkeypatch.setattr("panel.session.call_exaone_web_fact_check", fake_web_fact_check)
    monkeypatch.setattr(
        "panel.session.call_exaone_ui_director",
        lambda *args, **kwargs: ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=0.0,
            provider="exaone:ui-director",
        ),
    )
    session = MeetingSession(mock_ai=False, ai_provider="exaone")
    session.add_utterance("Claude Opus 4.8 벤치마크를 확인해야 합니다")
    event = TriggerEvent(
        panel_name="fact_checker",
        reason="수치/고유명사/검증 주장 감지",
        importance=2,
        priority="HIGH",
        utterance="Claude Opus 4.8 벤치마크를 확인해야 합니다",
    )

    output = session.run_panel(event)

    assert called["panel_name"] == "fact_checker"
    assert "Claude Opus 4.8" in called["prompt"]
    assert output.provider == "exaone+web"
    assert output.text == "근거부족: Opus 4.8 벤치마크 출처 불충분"
    assert output.sources[0]["title"] == "Anthropic"


def test_failed_exaone_fact_check_keeps_fallback_text(monkeypatch):
    def fake_web_fact_check(prompt, timeout_s, budget_gate=None):
        return ClaudeResponse(
            success=False,
            stdout="근거부족: 검색 출처를 찾지 못함",
            stderr="",
            elapsed_s=0.1,
            provider="exaone+web",
            sources=[],
        )

    monkeypatch.setattr("panel.session.call_exaone_web_fact_check", fake_web_fact_check)
    monkeypatch.setattr(
        "panel.session.call_exaone_ui_director",
        lambda *args, **kwargs: ClaudeResponse(
            success=False,
            stdout="",
            stderr="",
            elapsed_s=0.0,
            provider="exaone:ui-director",
        ),
    )
    session = MeetingSession(mock_ai=False, ai_provider="exaone")
    event = TriggerEvent(
        panel_name="fact_checker",
        reason="수치/고유명사/검증 주장 감지",
        importance=3,
        priority="HIGH",
        utterance="한은이 기준금리를 5연속 동결했습니다",
    )

    output = session.run_panel(event)

    assert output.provider == "exaone+web"
    assert output.text == "근거부족: 검색 출처를 찾지 못함"


def test_exaone_ui_director_updates_layout_and_panel_visuals(monkeypatch):
    def fake_exaone(panel_name, prompt, timeout_s, budget_gate=None):
        return ClaudeResponse(
            success=True,
            stdout="금리 리스크 중심 논의",
            stderr="",
            elapsed_s=0.1,
            provider="exaone",
        )

    def fake_ui_director(prompt, timeout_s, budget_gate=None):
        return ClaudeResponse(
            success=True,
            stdout=json.dumps({
                "layout_mode": "focus_b",
                "columns": [0.55, 1.3, 0.8],
                "panel_visuals": {
                    "fact_checker": {
                        "tone": "danger",
                        "emphasis": "pulse",
                        "density": "expanded",
                        "importance": 3,
                        "urgency": 3,
                    },
                    "timeline": {
                        "tone": "pending",
                        "emphasis": "strong",
                        "density": "expanded",
                        "importance": 2,
                        "urgency": 2,
                    },
                },
                "reason": "수치 검증이 우선",
                "expires_after_s": 20,
            }),
            stderr="",
            elapsed_s=0.2,
            provider="exaone:ui-director",
        )

    monkeypatch.setattr("panel.session.call_exaone", fake_exaone)
    monkeypatch.setattr("panel.session.call_exaone_ui_director", fake_ui_director)
    session = MeetingSession(mock_ai=False, ai_provider="exaone")
    event = TriggerEvent(
        panel_name="summarizer",
        reason="최근 흐름 요약",
        importance=2,
        priority="LOW",
        utterance="기준금리 동결과 환율 부담을 정리하겠습니다",
    )

    output = session.run_panel(event)
    state = session.state()

    assert output.provider == "exaone"
    assert state["layout"]["source"] == "exaone"
    assert state["layout"]["mode"] == "focus_b"
    assert state["layout"]["ggui_spec"]["columns"] == [0.55, 1.3, 0.8]
    assert state["panels"]["fact_checker"]["tone"] == "danger"
    assert state["panels"]["fact_checker"]["emphasis"] == "pulse"
    assert state["panels"]["fact_checker"]["density"] == "expanded"


def test_ai_provider_is_exposed_in_state():
    session = MeetingSession(ai_provider="exaone")

    assert session.state()["ai_provider"] == "exaone"

    session.set_ai_provider("mock")

    assert session.state()["ai_provider"] == "mock"

    session.set_ai_provider("unknown")

    assert session.state()["ai_provider"] == "exaone"


def test_mock_provider_uses_local_mock_without_budget_spend():
    session = MeetingSession(mock_ai=False, ai_provider="mock")
    event = TriggerEvent(
        panel_name="fact_checker",
        reason="수치/고유명사/검증 주장 감지",
        importance=2,
        priority="HIGH",
        utterance="비용이 30만 원입니다",
    )

    output = session.run_panel(event)

    assert output.provider == "mock"
    assert output.text
    assert session.state()["budget"]["call_count"] == 0


def test_layout_arbiter_expands_on_risk_and_can_be_disabled():
    session = MeetingSession(mock_ai=True)
    event = TriggerEvent(
        panel_name="devils_advocate",
        reason="합의/결정 신호 감지",
        importance=3,
        priority="MED",
        utterance="그럼 이 안으로 확정하죠",
    )

    output = session.run_panel(event)

    assert output.card_variant == "risk"
    assert session.state()["layout"]["mode"] == "critical"

    state = session.set_layout_arbiter_enabled(False)

    assert state["layout"]["arbiter_enabled"] is False
    assert state["layout"]["mode"] == "normal"


def test_reset_returns_session_to_initial_ready_state():
    session = MeetingSession(mock_ai=True, ai_provider="cli")
    session.start()
    session.set_mic_running(True)
    session.prepare_context(topic="금리 회의", goal="데모", terms=["한은"])
    session.set_panel_enabled("ideator", False)
    session.set_layout_arbiter_enabled(False)
    item, events = session.add_utterance("한은 기준금리 2.50% 확인 필요")
    assert item is not None
    fact_event = next(event for event in events if event.panel_name == "fact_checker")
    session.run_panel(fact_event)

    state = session.reset()

    assert state["running"] is False
    assert state["mic_running"] is False
    assert state["ai_provider"] == "mock"
    assert state["context"] == {"topic": "", "goal": "", "terms": []}
    assert state["transcript"] == []
    assert state["feed"] == []
    assert "ideator" in state["enabled_panels"]
    assert state["layout"]["arbiter_enabled"] is True
    assert state["layout"]["mode"] == "normal"
    assert state["panels"]["summarizer"]["text"] == "요약 대기"
    assert state["panels"]["fact_checker"]["text"] == "검증 주장 대기"
    assert state["panels"]["fact_checker"]["sources"] == []
