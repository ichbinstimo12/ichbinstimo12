from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class LLMResponse:
    """Normalized response from any LLM provider."""
    content: str
    model: str
    provider: str  # e.g. "ollama" or "anthropic"
    usage: Optional[Dict[str, int]] = None


class LLMProvider(ABC):
    """
    Abstraction over the LLM used by the Brain-Zone agents.

    The trading firm talks to this interface only. The concrete provider is
    selected via the LLM_PROVIDER environment variable ("ollama" or "anthropic").

    IMPORTANT: The LLM may only produce structured suggestions / thoughts.
    It never touches money. Execution stays in the deterministic Money-Zone.
    """

    provider_name: str = "unknown"

    @abstractmethod
    async def generate(
        self,
        system_prompt: str,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> LLMResponse:
        """Generate a completion.

        `messages` is a chat-style list, e.g.
        [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
        """

    @abstractmethod
    async def close(self) -> None:
        """Release resources (HTTP client, SDK client, ...)."""
        raise NotImplementedError
