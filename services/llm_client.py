# services/llm_client.py — LangChain chat model client for the local Ollama server
#
# Ollama exposes an OpenAI-compatible API, so we talk to it through
# langchain_openai.ChatOpenAI pointed at Ollama's /v1 endpoint. No real API
# key is needed — Ollama ignores it — but the OpenAI SDK requires a non-empty
# string.

from __future__ import annotations

from langchain_openai import ChatOpenAI

from services.config import require_llm_config

_llm: ChatOpenAI | None = None


def get_llm(**overrides) -> ChatOpenAI:
    """
    Return a cached ChatOpenAI client configured for the local Ollama server.

    Pass keyword overrides (e.g. temperature=0.2) to get a fresh, differently
    configured client instead of the cached default.
    """
    global _llm

    if overrides:
        config = require_llm_config()
        return ChatOpenAI(
            base_url=f"{config['ollama_api_base']}/v1",
            api_key="ollama",
            model=config["model"],
            **overrides,
        )

    if _llm is None:
        config = require_llm_config()
        _llm = ChatOpenAI(
            base_url=f"{config['ollama_api_base']}/v1",
            api_key="ollama",
            model=config["model"],
            temperature=0,
        )
    return _llm
