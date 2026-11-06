# agents/rtl_agent/copy_guard.py — the one place that says what a "copied
# answer" is and what the model is told about it.
#
# A fix or revise request hands the model a file and a list of problems; the
# model sometimes answers with that very file (timing_recovery, 2026-11-02
# 09:31: eight byte-identical answers while its notes said "I have rewritten
# the entire architecture"; each cost a compile and a 6-8 s dry run). Two
# places act on it and both use the rule below, so "identical" means one thing:
#   rtl_generator.RTLGenerator.generate  re-asks once, warmer, with the refusal line
#   rtl_linter.unchanged_on_retry_issue  reports a copy that still got through

from __future__ import annotations

# The re-ask uses a little randomness: at temperature 0 the same request
# returns the same copy (fix rounds 2 and 3 of each attempt were replays).
COPY_RETRY_TEMPERATURE = 0.2


def same_code(new: str | None, old: str | None) -> bool:
    """True when both are present and equal apart from blank ends."""
    return bool(new) and bool(old) and new.strip() == old.strip()


def copied_answer(rtl: dict | None, source: dict | None) -> bool:
    """True when the design `rtl` is the very file it was asked to change."""
    return same_code((rtl or {}).get("code"), (source or {}).get("code"))


def copy_refused_issue(lint_issues: list[dict] | None) -> dict:
    """The one extra problem line sent with the re-ask, naming the problems
    that are therefore still in the file."""
    codes = ", ".join(sorted({str(i.get("code") or "?") for i in lint_issues or []})) or "the downstream failures"
    return {"code": "unchanged_answer",
            "detail": f"Your previous answer changed 0 lines of previous_code; it was refused before any check "
                      f"ran, so the listed problems ({codes}) are still in the file. Change the quoted lines "
                      f"now: if a process body is only comments or the output valid is a constant '0', write "
                      f"that logic. Returning the same file again ends this attempt."}
