# agents/orchestrator_agent/requirement_id.py — the readable id of one
# requirement run, e.g.  4_bit_up_counter_T-11.35_D-16.10.2026
# Names projects/<id>/, so it must be a valid folder name on Windows
# (no ':' or '/', hence '.' inside the time and the date).

from __future__ import annotations

import json
import re
from datetime import datetime

MAX_NAME_LEN = 30


_JSON_WRAPPER_RE = re.compile(r'\s*\{\s*"requirement_text"\s*:\s*"(.*)"\s*\}\s*$', re.S)
_LABEL_RE = re.compile(r"\s*(?:requirement[ _]text|name)\s*:\s*", re.I)


def _strip_wrapper_and_label(text: str) -> str:
    """The run may be started with the text wrapped as {"requirement_text": "..."}
    and/or headed by a "Name:" label. Neither belongs in a folder name."""
    for _ in range(2):
        m = _JSON_WRAPPER_RE.match(text)
        if not m:
            break
        try:
            text = json.loads('"' + m.group(1) + '"')
        except ValueError:
            text = m.group(1)
    return _LABEL_RE.sub("", text, count=1)


def requirement_name(requirement_text: str, max_len: int = MAX_NAME_LEN) -> str:
    """A short folder-safe name taken from the requirement text (the spec's
    own entity name does not exist yet when the run starts)."""
    requirement_text = _strip_wrapper_and_label(requirement_text)
    name = re.sub(r"[^a-z0-9]+", "_", requirement_text.lower()).strip("_")
    return name[:max_len].rstrip("_") or "requirement"


def new_requirement_id(requirement_text: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{requirement_name(requirement_text)}_T-{now:%H.%M}_D-{now:%d.%m.%Y}"
