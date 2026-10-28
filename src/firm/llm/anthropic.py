import logging
import os
from typing import Dict, List, Optional

import anthropic

from src.firm.llm.base import LLMProvider, LLMResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AnthropicLLM")

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"


class AnthropicLLM(LLMProvider):
    """
    Cloud LLM provider backed by the Anthropic API.

    This is the **upgraded** provider of the trading firm (used after the
    local-Ollama phase). Requires an API key from
    https://console.anthropic.com -> API Keys and (for the best models) a
    paid / pro-tier Anthropic plan.

    The API key is passed exclusively via the ANTHROPIC_API_KEY environment
    variable (see .env). It is never logged or sent to agents.
    """

    provider_name = "anthropic"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "Anthropic API key missing. Set ANTHROPIC_API_KEY in .env or as an environment variable."
            )
        self.model = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self.max_tokens = max_tokens
        self._client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def generate(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        model = self.model
        resp = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens or self.max_tokens,
            system=system_prompt or "You are a trading firm agent.",
            messages=[
                {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
                for m in (messages or [])
            ],
            temperature=temperature,
        )
        text = ""
        for block in getattr(resp, "content", []) or []:
            block_type = getattr(block, "type", None)
            block_text = getattr(block, "text", None)
            if block_type in (None, "text") and block_text:
                text += block_text
        usage = getattr(resp, "usage", None)
        usage_dict = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }
        return LLMResponse(
            content=text.strip(),
            model=model,
            provider="anthropic",
            usage=usage_dict,
        )

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
