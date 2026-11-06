# agents/rtl_agent/rtl_generator.py — LLM tool: approved spec (+ loaded
# skills, + lint feedback when fixing) -> design dict.

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.json_utils import extract_json
from agents.rtl_agent.copy_guard import COPY_RETRY_TEMPERATURE, copied_answer, copy_refused_issue
from agents.rtl_agent.orchestrator_feedback import feedback_inputs, feedback_rules, has_feedback, revise_rules
from agents.rtl_agent.state import AGENT_NAME
from agents.vhdl_version import prepare_spec_for_rtl, resolve_vhdl_version
from observability.callback import ObservabilityCallbackHandler
from prompts.rtl_agent_prompt import RTL_AGENT_SYSTEM_PROMPT, RTL_FIX_ADDENDUM, RTL_SKILLS_ADDENDUM
from services.llm_client import get_llm


def parse_rtl_response(spec: dict, raw: str, finish_reason: str | None = None) -> dict:
    """Design JSON from the LLM's text.

    A reply the provider cut off (finish_reason "length") or that is not a
    JSON object has no usable code: it comes back with empty code and a note
    saying why, so the linter reports that once instead of checking the text
    (timing_recovery, 2026-11-02: a looping 99k-token reply was cut off
    mid-JSON, compiled, and reported as "markdown fences" and "no entity")."""
    raw = raw if isinstance(raw, str) else str(raw)
    if finish_reason == "length":
        why = f"the reply was cut off by the length limit after {len(raw):,} characters"
    else:
        try:
            rtl = extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            rtl = None
        if isinstance(rtl, dict):
            return rtl
        why = f"the reply was not valid JSON ({len(raw):,} characters)"
    return {
        "entity_name": spec.get("entity_name"),
        "code": "",
        "notes": [f"RTL agent did not return a usable design: {why}. Write the complete design again "
                  "as one JSON object, and keep it compact (no repeated declarations)."],
    }


class RTLGenerator:
    """Writes the design on the first pass; rewrites it against the linter's
    findings on later passes. An answer that is the very file it was asked to
    change is re-asked once with `retry_llm` (see copy_guard). Both clients
    are created lazily so tests can inject fakes."""

    def __init__(self, llm=None, retry_llm=None):
        self._llm = llm
        self._retry_llm = retry_llm
        self.finish_reason = None   # how the provider ended the last reply (see generate_raw)

    @property
    def llm(self):
        if self._llm is None:
            self._llm = get_llm()
        return self._llm

    @property
    def retry_llm(self):
        """A warmer client for the one re-ask after a copied answer."""
        if self._retry_llm is None:
            self._retry_llm = get_llm(temperature=COPY_RETRY_TEMPERATURE)
        return self._retry_llm

    def generate_raw(self, spec: dict, skills: dict[str, str] | None = None,
                     previous_rtl: dict | None = None, lint_issues: list[dict] | None = None,
                     attempt: int = 1, attempt_history: list[dict] | None = None,
                     starting_rtl: dict | None = None, llm=None) -> str:
        # previous_rtl / lint_issues: this run's own design that the linter
        # rejected (fix loop). starting_rtl: the design the orchestrator's
        # earlier call delivered, to be revised on a retry (repair loop).
        # The prompt names the edition (agents/vhdl_version.py decides it);
        # the spec itself is passed through unchanged.
        system = RTL_AGENT_SYSTEM_PROMPT.replace("{VHDL_VERSION}", resolve_vhdl_version(spec))
        if skills:
            system += RTL_SKILLS_ADDENDUM + "\n\n".join(f"### {name}\n{content}" for name, content in skills.items())
        feedback = has_feedback(attempt, attempt_history)        # the orchestrator called us again and says how it went
        if feedback:
            system += feedback_rules(attempt)
        fixing = previous_rtl is not None and bool(lint_issues)  # the linter rejected our last design in this run
        if fixing:
            system += RTL_FIX_ADDENDUM
        revising = feedback and not fixing and bool((starting_rtl or {}).get("code"))   # first write of a retry
        if revising:
            system += revise_rules()
        if not fixing and not feedback:
            user = json.dumps(spec, indent=2)
        else:
            payload = {"spec": spec}
            if feedback:
                payload.update(feedback_inputs(attempt_history, starting_rtl if revising else None))
            if fixing:
                payload.update(previous_code=previous_rtl.get("code"), lint_issues=lint_issues)
            user = json.dumps(payload, indent=2)
        handler = ObservabilityCallbackHandler(source=AGENT_NAME)
        response = (llm or self.llm).invoke(
            [SystemMessage(content=system), HumanMessage(content=user)],
            config={"callbacks": [handler]},
        )
        # "stop" = the model finished on its own, "length" = the length limit cut it off,
        # None = the provider did not say (fakes in tests)
        self.finish_reason = (getattr(response, "response_metadata", None) or {}).get("finish_reason")
        return response.content

    def generate(self, spec: dict, skills: dict[str, str] | None = None,
                 previous_rtl: dict | None = None, lint_issues: list[dict] | None = None,
                 attempt: int = 1, attempt_history: list[dict] | None = None,
                 starting_rtl: dict | None = None) -> dict:
        raw = self.generate_raw(spec, skills, previous_rtl, lint_issues, attempt, attempt_history, starting_rtl)
        rtl = parse_rtl_response(spec, raw, self.finish_reason)
        return self._re_ask_if_copied(rtl, spec, skills, previous_rtl, lint_issues, attempt, attempt_history,
                                      starting_rtl)

    def _re_ask_if_copied(self, rtl: dict, spec: dict, skills: dict[str, str] | None, previous_rtl: dict | None,
                          lint_issues: list[dict] | None, attempt: int, attempt_history: list[dict] | None,
                          starting_rtl: dict | None) -> dict:
        """One more call, warmer and with the refusal line added, when the
        answer is the very file it was asked to change: the fix loop's
        previous_rtl, or the orchestrator's starting_rtl on a retry's first
        write. The re-ask is always a fix request (previous_code + issues), so
        RTL_FIX_ADDENDUM comes along. One re-ask only: a second copy goes to
        the linter, which reports unchanged_on_retry and lets the stall rule
        end the attempt."""
        fixing = previous_rtl is not None and bool(lint_issues)
        source = previous_rtl if fixing else starting_rtl
        if not copied_answer(rtl, source):
            return rtl
        issues = [*(lint_issues or []), copy_refused_issue(lint_issues)]
        raw = self.generate_raw(spec, skills, source, issues, attempt, attempt_history, starting_rtl,
                                llm=self.retry_llm)
        again = parse_rtl_response(spec, raw, self.finish_reason)
        notes = again.get("notes") if isinstance(again.get("notes"), list) else []
        again["notes"] = [*notes, "re-asked once: the first answer was previous_code unchanged"]
        return again
