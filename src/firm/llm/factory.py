import logging
import os
from typing import Optional

from src.firm.llm.anthropic import AnthropicLLM
from src.firm.llm.base import LLMProvider
from src.firm.llm.ollama import OllamaLLM

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LLMFactory")

DEFAULT_PROVIDER = "ollama"


def create_llm_provider(provider_name: Optional[str] = None, model: Optional[str] = None) -> LLMProvider:
    """
    Factory for the LLM provider used by the Brain-Zone agents.

    Selection via env var LLM_PROVIDER (or explicit argument):
        - "ollama"    -> local Ollama instance (default, free, offline-first)
        - "anthropic" -> Anthropic API via ANTHROPIC_API_KEY (upgrade path)

    `model` optionally overrides the default model for a single agent, so a
    role config can run e.g. a larger local model for the CEO.
    """
    name = (provider_name or os.getenv("LLM_PROVIDER", DEFAULT_PROVIDER) or DEFAULT_PROVIDER).strip().lower()
    if name == "ollama":
        logger.info("LLM provider: ollama (base_url=%s, model=%s)", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"), model or os.getenv("OLLAMA_MODEL", "llama3.1:8b"))
        return OllamaLLM(model=model)
    if name == "anthropic":
        logger.info("LLM provider: anthropic (model=%s)", model or os.getenv("ANTHROPIC_MODEL", "claude-opus-5-5"))
        return AnthropicLLM(model=model)
    raise ValueError(f"Unknown LLM_PROVIDER '{name}'. Valid options: 'ollama', 'anthropic'.")


def get_llm_provider() -> LLMProvider:
    return create_llm_provider()
