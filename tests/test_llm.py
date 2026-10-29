import json
import os
import pytest
import httpx
from src.firm.bus.event_store import EventStore
from src.firm.llm import AnthropicLLM, OllamaLLM, create_llm_provider, get_llm_provider
from src.firm.llm.base import LLMProvider, LLMResponse
from src.firm.agents.runtime import AgentRuntime, AgentConfig

class StubLLM(LLMProvider):
    provider_name = "stub"

    async def generate(self, system_prompt, messages, max_tokens=None, temperature=None):
        return LLMResponse(content='{"summary": "stub thought", "actions": [], "lesson": null}',
                           model="stub", provider="stub")

    async def close(self):
        pass

@pytest.fixture
def event_store(tmp_path):
    return EventStore(str(tmp_path / "test_llm.db"))

@pytest.mark.asyncio
async def test_agent_runtime_uses_llm_provider(event_store):
    runtime = AgentRuntime(event_store, llm=StubLLM())
    assert runtime.llm.provider_name == "stub"
    runtime.active_agents["QuantDev"] = AgentConfig(
        name="QuantDev",
        role="Developer",
        system_prompt="You build strategies.",
        tool_allowlist=["get_data"],
        model="llama3.1",
    )

    result = await runtime.step("QuantDev", "Market is bullish", "task-llm-1")
    assert result["status"] == "success"

def test_factory_default_is_ollama(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    provider = create_llm_provider()
    assert isinstance(provider, OllamaLLM)
    assert provider.provider_name == "ollama"

def test_factory_unknown_provider_raises():
    with pytest.raises(ValueError):
        create_llm_provider("not-a-provider")

def test_factory_selects_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    provider = create_llm_provider("anthropic")
    assert isinstance(provider, AnthropicLLM)

def test_factory_anthropic_requires_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        create_llm_provider("anthropic")

def test_ollama_generate_parses_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "llama3.1:8b"
        assert body["stream"] is False
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"]
        assert body["messages"][1]["role"] == "user"
        return httpx.Response(200, json={
            "model": "llama3.1:8b",
            "message": {"role": "assistant", "content": "  The market looks bullish.  "},
            "done": True,
            "prompt_eval_count": 12,
            "eval_count": 34,
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    provider = OllamaLLM(base_url="http://localhost:11434", model="llama3.1:8b", client=client)

    import asyncio
    response = asyncio.run(provider.generate(
        system_prompt="You are a quant.",
        messages=[{"role": "user", "content": "Is BTCUSD bullish?"}],
        max_tokens=512,
    ))
    assert response.content == "The market looks bullish."
    assert response.provider == "ollama"
    assert response.usage == {"input_tokens": 12, "output_tokens": 34}
    asyncio.run(client.aclose())



@pytest.mark.asyncio
async def test_ollama_ping_checks_server_and_model():
    import httpx
    from src.firm.llm.ollama import OllamaLLM

    def handler(req):
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:14b"}]})
    llm = OllamaLLM(base_url="http://x", model="qwen2.5:14b", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await llm.ping()
    llm.model = "llama3.1:8b"
    with pytest.raises(RuntimeError, match="not pulled"):
        await llm.ping()
    await llm.close()


def test_ollama_context_grows_with_the_prompt(monkeypatch):
    from src.firm.llm.ollama import OllamaLLM
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    assert OllamaLLM.context_size([{"content": "x" * 1000}], 1024) == 8192
    assert OllamaLLM.context_size([{"content": "x" * 20000}], 4000) == 16384           # ~9.7k tokens needed
    assert OllamaLLM.context_size([{"content": "x" * 10**6}], 4000) == 32768          # capped
    monkeypatch.setenv("OLLAMA_NUM_CTX", "12000")
    assert OllamaLLM.context_size([{"content": "x"}], 10) == 12000


@pytest.mark.asyncio
async def test_ollama_request_carries_num_ctx():
    import httpx
    from src.firm.llm.ollama import OllamaLLM
    sent = {}

    def handler(req):
        sent.update(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "{}"}, "model": "m"})
    llm = OllamaLLM(base_url="http://x", model="m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await llm.generate("sys", [{"role": "user", "content": "hi"}], max_tokens=4000, json_mode=True)
    assert sent["options"]["num_ctx"] >= 8192 and sent["format"] == "json"
    await llm.close()
