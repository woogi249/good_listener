from panel.budget import BudgetConfig, BudgetGate


def test_budget_gate_reserves_and_settles_usage():
    gate = BudgetGate(BudgetConfig(max_budget_krw=1000, min_estimated_call_krw=2.0))
    messages = [{"role": "user", "content": "최근 발화"}]

    decision = gate.reserve_messages(messages, feature="summarizer")

    assert decision.allowed is True
    assert gate.state()["call_count"] == 1
    assert gate.state()["used_krw"] == 2.0

    gate.settle(
        decision.reservation,
        {"prompt_tokens": 1000, "completion_tokens": 100},
    )

    assert gate.state()["used_krw"] < 2.0


def test_budget_gate_blocks_when_meeting_cap_would_be_exceeded():
    gate = BudgetGate(BudgetConfig(max_budget_krw=1.0, min_estimated_call_krw=2.0))

    decision = gate.reserve_messages([{"role": "user", "content": "x"}], feature="fact")

    assert decision.allowed is False
    state = gate.state()
    assert state["blocked_count"] == 1
    assert state["last_block_reason"] == "회의 예산 한도 초과"


def test_budget_gate_blocks_per_minute_limit():
    gate = BudgetGate(
        BudgetConfig(
            max_budget_krw=100,
            max_calls_per_meeting=10,
            max_calls_per_minute=1,
        )
    )

    first = gate.reserve_messages([{"role": "user", "content": "x"}], feature="a", now=10.0)
    second = gate.reserve_messages([{"role": "user", "content": "x"}], feature="b", now=11.0)

    assert first.allowed is True
    assert second.allowed is False
    assert second.reason == "분당 LLM 호출 한도 초과"
