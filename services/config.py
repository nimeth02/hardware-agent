# services/config.py — Env-backed configuration for the LLM client
#
# Loads the repo-root `.env` and provides helpers for required LLM settings
# (provider, base URL, API key, model). Provider is chosen by LLM_PROVIDER.
#
# Important: we intentionally *do not* hard-fail at import time. Modules may
# import this without ever calling the LLM. We validate when the values are
# actually requested.

from __future__ import annotations

import os
from pathlib import Path
from typing import TypedDict

from dotenv import load_dotenv


class LLMConfig(TypedDict):
    provider: str                  # "ollama" or "openrouter"
    base_url: str                  # OpenAI-compatible root, ending in /v1
    api_key: str
    model: str
    chat_completions_url: str
    extra_headers: dict[str, str]
    extra_body: dict          # provider-specific request options
    timeout: float            # seconds before one request gives up


REPO_ROOT = Path(__file__).resolve().parents[1]  # Hardware Agent/

# Load env from an explicit path so it works regardless of current working dir.
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)


_LLM_CONFIG: LLMConfig | None = None

PROVIDERS = ("ollama", "openrouter")


def _normalize_base_url(base: str) -> str:
    return base.strip().rstrip("/")


def _v1(base: str) -> str:
    # Accept both http://host:11434 and http://host:11434/v1.
    return base if base.endswith("/v1") else base + "/v1"


def _missing_error(names: list[str], provider: str) -> RuntimeError:
    return RuntimeError(
        f"Missing required LLM env var(s) for provider '{provider}': {', '.join(names)}. "
        f"Create {REPO_ROOT}/.env and set them (see .env.example)."
    )


def require_llm_config() -> LLMConfig:
    """
    Validate env and return the LLM configuration for the provider chosen by
    LLM_PROVIDER ("ollama" or "openrouter"). Switching provider is one line in .env.
    Raises a clear error if required env vars are missing.
    """
    global _LLM_CONFIG
    if _LLM_CONFIG is not None:
        return _LLM_CONFIG

    provider = os.getenv("LLM_PROVIDER", "ollama").strip().lower()
    if provider not in PROVIDERS:
        raise RuntimeError(
            f"LLM_PROVIDER='{provider}' is not supported. Use one of: {', '.join(PROVIDERS)}."
        )

    extra_headers: dict[str, str] = {}
    extra_body: dict = {}
    if provider == "openrouter":
        api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
        model = (os.getenv("OPENROUTER_MODEL") or "").strip()
        missing = [n for n, v in (("OPENROUTER_API_KEY", api_key), ("OPENROUTER_MODEL", model)) if not v]
        if missing:
            raise _missing_error(missing, provider)
        base_url = _v1(_normalize_base_url(
            os.getenv("OPENROUTER_API_BASE") or "https://openrouter.ai/api"
        ))
        # Optional: lets OpenRouter show this app's name in its dashboard.
        extra_headers = {"X-Title": "Hardware Agent"}
        # Qwen3.8 "thinks" for thousands of tokens before answering (minutes per
        # call). Off by default; set OPENROUTER_REASONING=on to allow it.
        if (os.getenv("OPENROUTER_REASONING") or "off").strip().lower() not in ("on", "true", "1"):
            extra_body = {"reasoning": {"enabled": False}}
    else:
        ollama_api_base = (os.getenv("OLLAMA_API_BASE") or "").strip()
        model = (os.getenv("LLM_MODEL") or "").strip()
        missing = [n for n, v in (("OLLAMA_API_BASE", ollama_api_base), ("LLM_MODEL", model)) if not v]
        if missing:
            raise _missing_error(missing, provider)
        base_url = _v1(_normalize_base_url(ollama_api_base))
        api_key = "ollama"  # Ollama ignores it, but the OpenAI SDK needs a non-empty string.

    _LLM_CONFIG = {
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "chat_completions_url": base_url + "/chat/completions",
        "extra_headers": extra_headers,
        "extra_body": extra_body,
        "timeout": float(os.getenv("LLM_TIMEOUT_SECONDS") or 300),
    }
    return _LLM_CONFIG


def require_ollama_chat_completions_url() -> str:
    """Chat-completions URL of the ACTIVE provider (name kept for old callers)."""
    return require_llm_config()["chat_completions_url"]


def require_llm_model() -> str:
    return require_llm_config()["model"]


# Optional Vivado pinning (used by agents/rtl_agent/rtl_linter.py)
VIVADO_SETTINGS64 = os.getenv("VIVADO_SETTINGS64", "").strip()

# Target FPGA part for synthesis/implementation (agents/implementation_agent).
# Default is the Artix-7 on a Basys 3, which the free WebPACK licence covers.
VIVADO_PART = os.getenv("VIVADO_PART", "xc7a35tcpg236-1").strip()
