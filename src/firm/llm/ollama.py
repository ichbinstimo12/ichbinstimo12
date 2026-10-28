import logging
import os
from typing import Dict, List, Optional

import httpx

from src.firm.llm.base import LLMProvider, LLMResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OllamaLLM")

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1"


class OllamaLLM(LLMProvider):
    """
    Local LLM provider backed by Ollama (default for the trading firm).

    Runs fully offline / local-first: no API key needed, no cloud costs.
    Used until the firm is upgraded to the Anthropic API.

    Ollama exposes an OpenAI-compatible chat endpoint at:
        POST {base_url}/api/chat
    with body {"model": ..., "messages": [...], "stream": false}.
    """

    provider_name = "ollama"

    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 120.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        self.timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def generate(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        ollama_messages: List[Dict[str, str]] = []
        if system_prompt:
            ollama_messages.append({"role": "system", "content": system_prompt})
        for msg in messages or []:
            role = str(msg.get("role") or "user").lower()
            if role not in ("system", "user", "assistant"):
                role = "user"
            ollama_messages.append({"role": role, "content": str(msg.get("content") or "")})

        payload: Dict[str, object] = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["num_predict"] = max_tokens

        response = await self._client.post(
            f"{self.base_url}/api/chat",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        message = data.get("message") or {}
        content = str(message.get("content") or "")
        usage = {
            "input_tokens": int(data.get("prompt_eval_count") or 0),
            "output_tokens": int(data.get("eval_count") or 0),
        }
        return LLMResponse(
            content=content.strip(),
            model=str(data.get("model") or self.model),
            provider="ollama",
            usage=usage,
        )

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
