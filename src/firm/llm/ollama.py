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
        timeout: Optional[float] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL)).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        # A 14B model on a CPU-only PC easily needs several minutes per answer.
        self.timeout = timeout if timeout is not None else float(os.getenv("OLLAMA_TIMEOUT", "600"))
        self._client = client or httpx.AsyncClient(timeout=self.timeout)

    async def generate(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        json_mode: bool = False,
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
        # Ollama reads sampling settings only from "options" (top-level keys are ignored).
        options: Dict[str, object] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        if options:
            payload["options"] = options
        if json_mode:
            payload["format"] = "json"

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

    async def ping(self) -> None:
        """Server up AND the configured model pulled (GET /api/tags, no generation)."""
        r = await self._client.get(f"{self.base_url}/api/tags", timeout=10)
        r.raise_for_status()
        names = {m.get("name", "") for m in (r.json().get("models") or [])}
        base = self.model.split(":")[0]
        if self.model not in names and not any(n.split(":")[0] == base for n in names):
            raise RuntimeError(f"model {self.model} not pulled (ollama pull {self.model})")

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
