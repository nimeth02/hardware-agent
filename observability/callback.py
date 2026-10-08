# observability/callback.py — LangChain callback handler that records every
# LLM call's input, output, and timing.
#
# Attach it at the call site (`llm.invoke(messages, config={"callbacks": [...]})`)
# rather than baking it into the client — keeps the LLM client itself agnostic
# of observability, and lets each agent tag its calls with its own `source`.

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage

from observability.storage import save_trace


def _message_to_dict(msg: BaseMessage) -> dict:
    return {"role": msg.type, "content": msg.content}


class ObservabilityCallbackHandler(BaseCallbackHandler):
    """Records each chat-model call's input, output, timing, and errors."""

    def __init__(self, source: str = "unknown"):
        self.source = source
        self._starts: dict[str, dict] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        model = None
        if serialized:
            kwargs_ = serialized.get("kwargs") or {}
            model = kwargs_.get("model") or kwargs_.get("model_name")
        self._starts[str(run_id)] = {
            "started_at": time.perf_counter(),
            "messages": [_message_to_dict(m) for m in messages[0]] if messages else [],
            "model": model,
        }

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        start = self._starts.pop(str(run_id), None)
        if start is None:
            return
        duration_ms = (time.perf_counter() - start["started_at"]) * 1000

        output_text = ""
        try:
            output_text = response.generations[0][0].text
        except Exception:
            pass

        usage = None
        try:
            usage = response.llm_output.get("token_usage") if response.llm_output else None
        except Exception:
            pass

        save_trace(
            {
                "source": self.source,
                "model": start.get("model"),
                "input_messages": start["messages"],
                "output_text": output_text,
                "duration_ms": duration_ms,
                "usage": usage,
                "error": None,
            }
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        start = self._starts.pop(str(run_id), None)
        if start is None:
            return
        duration_ms = (time.perf_counter() - start["started_at"]) * 1000

        save_trace(
            {
                "source": self.source,
                "model": start.get("model"),
                "input_messages": start["messages"],
                "output_text": None,
                "duration_ms": duration_ms,
                "usage": None,
                "error": str(error),
            }
        )
