# services/config.py — Env-backed configuration for the LLM client
#
# Loads the repo-root `.env` and provides helpers for required LLM settings
# (Ollama API base + model).
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
    ollama_api_base: str
    ollama_chat_completions_url: str
    model: str


REPO_ROOT = Path(__file__).resolve().parents[1]  # Hardware Agent/

# Load env from an explicit path so it works regardless of current working dir.
load_dotenv(dotenv_path=REPO_ROOT / ".env", override=False)


_LLM_CONFIG: LLMConfig | None = None


def _normalize_base_url(base: str) -> str:
    return base.strip().rstrip("/")


def _compute_chat_completions_url(ollama_api_base: str) -> str:
    # Allow users to set either:
    # - OLLAMA_API_BASE=http://host:11434     (recommended)
    # - OLLAMA_API_BASE=http://host:11434/v1  (tolerated)
    if ollama_api_base.endswith("/v1"):
        return ollama_api_base + "/chat/completions"
    return ollama_api_base + "/v1/chat/completions"


def require_llm_config() -> LLMConfig:
    """
    Validate env and return normalized LLM configuration.
    Raises a clear error if required env vars are missing.
    """
    global _LLM_CONFIG
    if _LLM_CONFIG is not None:
        return _LLM_CONFIG

    ollama_api_base = os.getenv("OLLAMA_API_BASE")
    model = os.getenv("LLM_MODEL")

    missing: list[str] = []
    if not ollama_api_base:
        missing.append("OLLAMA_API_BASE")
    if not model:
        missing.append("LLM_MODEL")

    if missing:
        missing_str = ", ".join(missing)
        raise RuntimeError(
            f"Missing required LLM env var(s): {missing_str}. "
            f"Create {REPO_ROOT}/.env and set them. Example:\n"
            "  OLLAMA_API_BASE=http://localhost:11434\n"
            "  LLM_MODEL=qwen3-coder-next\n"
        )

    ollama_api_base_norm = _normalize_base_url(ollama_api_base)
    model_norm = model.strip()

    _LLM_CONFIG = {
        "ollama_api_base": ollama_api_base_norm,
        "ollama_chat_completions_url": _compute_chat_completions_url(ollama_api_base_norm),
        "model": model_norm,
    }
    return _LLM_CONFIG


def require_ollama_chat_completions_url() -> str:
    return require_llm_config()["ollama_chat_completions_url"]


def require_llm_model() -> str:
    return require_llm_config()["model"]


# Optional Vivado pinning (used by agents/rtl_agent/rtl_linter.py)
VIVADO_SETTINGS64 = os.getenv("VIVADO_SETTINGS64", "").strip()

# Target FPGA part for synthesis/implementation (agents/implementation_agent).
# Default is the Artix-7 on a Basys 3, which the free WebPACK licence covers.
VIVADO_PART = os.getenv("VIVADO_PART", "xc7a35tcpg236-1").strip()
