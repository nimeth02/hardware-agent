# agents/json_utils.py — tolerant JSON extraction from LLM chat responses

from __future__ import annotations

import json


def extract_json(text: str) -> dict:
    """
    Parse a JSON object out of an LLM response.

    Tolerates a ```json ... ``` fence around the object, and falls back to
    slicing between the first "{" and last "}" if the model added
    leading/trailing prose despite instructions to return only JSON.
    """
    stripped = text.strip()

    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(stripped[start : end + 1])
        raise
