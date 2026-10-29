import logging
import os
from typing import Dict, List, Optional

import anthropic

from src.firm.llm.base import LLMProvider, LLMResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AnthropicLLM")

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"


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
        max_tokens: int = 4000,
    ):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "Anthropic API key missing. Set ANTHROPIC_API_KEY in .env or as an environment variable."
            )
        self.model = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
        self.max_tokens = max_tokens
        # Agent tasks are short structured reviews; "low" keeps cost and latency down. Override via env.
        self.effort = os.getenv("ANTHROPIC_EFFORT", "low")
        self._client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def generate(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        # `temperature` is accepted for interface compatibility but not sent: current Claude models
        # reject sampling parameters. `json_mode` is enforced through the agents' output contract.
        resp = await self._client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens or self.max_tokens,
            system=system_prompt or "You are a trading firm agent.",
            messages=[
                {"role": "assistant" if m.get("role") == "assistant" else "user", "content": str(m.get("content") or "")}
                for m in (messages or []) if m.get("role") != "system"
            ],
            output_config={"effort": self.effort},
            # If a safety classifier declines, the API retries on a fallback model inside the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            raise RuntimeError("Anthropic model declined the request (stop_reason=refusal)")
        text = "".join(
            getattr(block, "text", "") or ""
            for block in getattr(resp, "content", []) or []
            if getattr(block, "type", None) == "text"
        )
        usage = getattr(resp, "usage", None)
        usage_dict = {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        }
        return LLMResponse(
            content=text.strip(),
            model=str(getattr(resp, "model", None) or self.model),
            provider="anthropic",
            usage=usage_dict,
        )

    async def ping(self) -> None:
        """Key valid and model available (models endpoint, no tokens billed)."""
        await self._client.models.retrieve(self.model, timeout=15)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass
