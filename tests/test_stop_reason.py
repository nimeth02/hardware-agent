from agents.orchestrator_agent.stop_reason import stop_reason


def test_step_limit_is_explained_in_plain_words():
    text = stop_reason({}, {}, "orchestration failed: Recursion limit of 51 reached")
    assert "25 steps" in text


def test_crash_text_loses_the_technical_prefix():
    assert stop_reason({}, {}, "orchestration failed: Ollama down") == "The run crashed before it finished: Ollama down"


def test_unknown_status_is_called_out():
    assert "unknown status" in stop_reason({"status": "DONE"}, {}, None)


def test_manager_reason_is_used_when_given():
    assert stop_reason({"status": "NEEDS_REVIEW", "stop_reason": "RTL kept failing lint"}, {}, None) == "RTL kept failing lint"


def test_missing_reason_comes_from_the_last_unfinished_helper():
    stages = {"generate_rtl": {"status": "NEEDS_REVIEW", "attempt_history": [
        {"attempt": 3, "status": "NEEDS_REVIEW", "error": "Iteration limit (10) reached"}]}}
    text = stop_reason({"status": "NEEDS_REVIEW"}, {"stages": stages}, None)
    assert "generate_rtl attempt 3 ended NEEDS_REVIEW: Iteration limit (10) reached" == text
