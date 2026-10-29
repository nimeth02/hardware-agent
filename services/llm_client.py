# services/llm_client.py — LangChain chat model client (Ollama or OpenRouter)
#
# Both providers speak the OpenAI-compatible API, so we talk to either through
# langchain_openai.ChatOpenAI. Which one is used is decided by LLM_PROVIDER in
# .env ("ollama" or "openrouter") — see services/config.py.

from __future__ import annotations

from langchain_openai import ChatOpenAI

from services.config import require_llm_config

_llm: ChatOpenAI | None = None


def _build(**kwargs) -> ChatOpenAI:
    config = require_llm_config()
    params = dict(
        base_url=config["base_url"],
        api_key=config["api_key"],
        model=config["model"],
        default_headers=config["extra_headers"] or None,
        extra_body=config["extra_body"] or None,
        timeout=config["timeout"],
    )
    params.update(kwargs)
    return ChatOpenAI(**params)


def get_llm(**overrides) -> ChatOpenAI:
    """
    Return a cached ChatOpenAI client for the configured provider.

    Pass keyword overrides (e.g. temperature=0.2) to get a fresh, differently
    configured client instead of the cached default.
    """
    global _llm

    if overrides:
        return _build(**overrides)

    if _llm is None:
        _llm = _build(temperature=0)
    return _llm


def get_llm_for_loop(**overrides) -> ChatOpenAI:
    """A client with its OWN async HTTP connection pool, for code that runs
    on a short-lived or private event loop (asyncio.run, a session thread).

    The cached client — and every ChatOpenAI built with default settings —
    shares one async httpx pool; a keep-alive connection opened on one loop
    raises "Event loop is closed" when first reused on another. Observed as
    the test-vector agent's first MATLAB attempt failing every run."""
    import httpx

    kwargs = dict(
        temperature=0,
        http_async_client=httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0)),
    )
    kwargs.update(overrides)
    return _build(**kwargs)
