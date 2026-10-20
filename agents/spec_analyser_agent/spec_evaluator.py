# agents/spec_evaluator.py — deterministic structural evaluation of a spec
# JSON. No LLM call: just field/shape checks that the analyzer's own JSON
# output can still violate (missing port width, no reset polarity, etc.)
# despite the prompt's instructions.

from __future__ import annotations

import re

from agents.vhdl_version import SPEC_FIELD, SUPPORTED_VERSIONS, normalize_version

# A plain number ("8"), a range ("[7:0]", "[N-1:0]"), or a symbolic width
# ("N", "DATA_WIDTH", "N+1") — generics/parameters are legitimate in VHDL.
# Something like "8bits" or "8 bit" is still flagged.
_WIDTH_RE = re.compile(r"^\d+$|^\[[^\[\]:]+:[^\[\]:]+\]$|^[A-Za-z_][\w+\-*/()]*$")


def evaluate_spec(spec: dict) -> list[dict]:
    """Return a list of {"code": ..., "detail": ...} issues found in `spec`.
    An empty list means the spec looks structurally complete."""
    issues: list[dict] = []

    def issue(code: str, detail: str) -> None:
        issues.append({"code": code, "detail": detail})

    if not isinstance(spec, dict):
        issue("not_object", "Spec is not a JSON object.")
        return issues

    if not spec.get("entity_name"):
        issue("missing_entity_name", "No entity_name given.")

    ports = spec.get("ports")
    if not ports:
        issue("no_ports", "No ports are defined.")
    elif isinstance(ports, list):
        for i, p in enumerate(ports):
            label = p.get("name") if isinstance(p, dict) and p.get("name") else f"#{i + 1}"
            if not isinstance(p, dict):
                issue("bad_port", f"Port {label} is not a valid object.")
                continue
            if not p.get("name"):
                issue("port_missing_name", f"Port {label} has no name.")
            if p.get("direction") not in ("input", "output"):
                issue("port_bad_direction", f"Port '{label}' has a missing/invalid direction.")
            width = p.get("width")
            if not width:
                issue("port_missing_width", f"Port '{label}' has no width.")
            elif not _WIDTH_RE.match(str(width).strip()):
                issue(
                    "port_odd_width",
                    f"Port '{label}' width '{width}' isn't a plain number or [m:n] range — double-check it.",
                )

    clock = spec.get("clock") or {}
    if clock.get("required") and not clock.get("name"):
        issue("missing_clock_name", "A clock is required but no clock signal name was given.")

    reset = spec.get("reset") or {}
    if reset.get("required"):
        if not reset.get("name"):
            issue("missing_reset_name", "A reset is required but no reset signal name was given.")
        if reset.get("type") not in ("synchronous", "asynchronous"):
            issue("missing_reset_type", "Reset type (synchronous/asynchronous) is missing or invalid.")
        if reset.get("active") not in ("high", "low"):
            issue("missing_reset_polarity", "Reset polarity (active-high/active-low) is missing or invalid.")

    if not spec.get("behavior"):
        issue("no_behavior", "No functional behavior is described.")

    # Absent is fine (the generator fills the default); present-but-garbage is not.
    version = spec.get(SPEC_FIELD)
    if version is not None and normalize_version(version) is None:
        issue("bad_vhdl_version",
              f"vhdl_version '{version}' is not one of {', '.join(SUPPORTED_VERSIONS)}.")

    for amb in spec.get("ambiguities") or []:
        issue("ambiguity", str(amb))

    if spec.get("status") == "needs_clarification":
        issue("needs_clarification", "The spec analyzer marked this spec as needing clarification.")

    return issues
