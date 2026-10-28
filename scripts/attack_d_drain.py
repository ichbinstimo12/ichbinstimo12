import asyncio
import logging
from src.firm.agents.runtime import AgentRuntime, AgentConfig
from src.firm.bus.event_store import EventStore
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("AttackD")

class BudgetedAgentRuntime(AgentRuntime):
    """
    Extended runtime to implement the token budget enforcement.
    """
    def __init__(self, event_store: EventStore):
        super().__init__(event_store)
        self.token_usage: Dict[str, int] = {}

    async def step(self, agent_name: str, observation: str, task_id: str):
        config = self.active_agents.get(agent_name)
        if not config:
            raise ValueError(f"Agent {agent_name} not loaded.")

        # 1. Check Budget BEFORE execution
        current_usage = self.token_usage.get(agent_name, 0)
        if current_usage >= config.daily_token_budget:
            logger.error(f"BUDGET EXCEEDED: Agent {agent_name} has used {current_usage} tokens. Halting execution.")
            return {"status": "HALTED", "reason": "Token budget exceeded"}

        # 2. Simulate execution and token cost
        # In a real scenario, we get this from the Anthropic API response
        cost_of_this_step = 1000 # Simulating 1k tokens per step
        self.token_usage[agent_name] = current_usage + cost_of_this_step

        # Call the original step logic (Observation -> Thought -> Act)
        return await super().step(agent_name, observation, task_id)

async def attack_brain_drain():
    print("\n[ATTACK D] Simulating Agent Token Exhaustion...")

    # Setup
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "budget_test.db"
        event_store = EventStore(str(db_path))
        runtime = BudgetedAgentRuntime(event_store)

        # Create agent with a very small budget for testing
        agent_name = "ExpensiveAgent"
        config = AgentConfig(
            name=agent_name,
            role="Research",
            system_prompt="You are very talkative.",
            tool_allowlist=["get_data"],
            daily_token_budget=2500 # Limit to 2.5k tokens
        )
        runtime.active_agents[agent_name] = config

        # Simulate a loop of requests
        steps_taken = 0
        for i in range(10):
            res = await runtime.step(agent_name, f"Observation {i}", f"task-{i}")
            if res["status"] == "HALTED":
                print(f"Agent halted at step {i+1} as expected.")
                break
            steps_taken += 1

        if steps_taken < 10:
            print(f"RESULT: ✅ SUCCESS. Agent was halted after {steps_taken} steps due to budget.")
            return True
        else:
            print("RESULT: ❌ FAILURE. Agent exceeded budget without being halted.")
            return False

if __name__ == "__main__":
    asyncio.run(attack_brain_drain())
