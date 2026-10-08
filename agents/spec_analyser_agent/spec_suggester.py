# agents/spec_suggester.py — turns structural issues from spec_evaluator
# into short, actionable suggestions for the human reviewer.

from __future__ import annotations

_SUGGESTION_TEMPLATES = {
    "not_object": "{detail}",
    "missing_entity_name": "Give the design an entity_name.",
    "no_ports": "Add the input/output ports for this entity.",
    "bad_port": "Fix the malformed port entry ({detail}).",
    "port_missing_name": "{detail} Give it a name.",
    "port_bad_direction": "{detail} Set direction to 'input' or 'output'.",
    "port_missing_width": '{detail} Give it a width, e.g. "1" or "[7:0]".',
    "port_odd_width": "{detail}",
    "missing_clock_name": "{detail} Name it, e.g. 'clk'.",
    "missing_reset_name": "{detail} Name it, e.g. 'rst'.",
    "missing_reset_type": "{detail}",
    "missing_reset_polarity": "{detail}",
    "no_behavior": "{detail} Describe what the design should do, step by step.",
    "ambiguity": "Resolve this ambiguity: {detail}",
    "needs_clarification": "{detail}",
}


def generate_suggestions(issues: list[dict]) -> list[str]:
    """Map structural issues (from spec_evaluator.evaluate_spec) to short,
    actionable suggestions for the human reviewer."""
    suggestions = []
    for issue in issues:
        template = _SUGGESTION_TEMPLATES.get(issue["code"], "{detail}")
        suggestions.append(template.format(detail=issue["detail"]))
    return suggestions
