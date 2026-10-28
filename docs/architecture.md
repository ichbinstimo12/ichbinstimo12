# Architecture & Feasibility: Autonomous Trading Firm

## 1. Technical Blueprint

### 1.1 System Topology
The system is split into two strictly isolated zones to ensure that no LLM error can compromise capital.

**Brain-Zone (Agent Runtime)**
- **Agent Orchestrator**: Loads role YAMLs, manages the LLM loop (Observe $\rightarrow$ Think $\rightarrow$ Act), and handles tool-calling.
- **Memory Manager**: Manages the SQLite-based episodic and semantic memory (Lessons).
- **Task Queue**: Handles the distribution of assignments from the CEO to other agents.
- **CEO Hiring Module**: Interface for submitting Hiring Requests and new Tool Rights for user approval.

**Money-Zone (Deterministic Core)**
- **BrokerClient**: Async wrapper for Capital.com API. Handles session management, token refresh, and WebSocket streaming for real-time prices.
- **RiskGate**: The ultimate firewall. Validates every `TradeProposal` against `limits.toml`, the **User-Approved Whitelist**, and real-time account data.
- **ExecutionGateway**: Ensures order idempotency using `dealReference` and manages the lifecycle of an order from proposal to fill.
- **Reconciler**: A background loop that polls the Broker every $N$ seconds to ensure the internal state exactly matches the Broker's positions.
- **KillSwitch**: High-priority interrupt. 
    - **Logic**: Sends `flatten_all` commands.
    - **Resilience**: Monitors token validity; if a token expires during a kill, it immediately refreshes the session and retries.
    - **Termination**: Continues aggressive retries until the Broker API explicitly confirms all positions are "flat".

### 1.2 Data Model & Event Store
The system uses a **Single Source of Truth** via an append-only SQLite Event Store (WAL mode).

**Event Table Schema:**
- `id` (UUID, PK)
- `ts` (Timestamp, UTC)
- `agent_id` (String)
- `type` (EventType: `THOUGHT`, `TOOL_CALL`, `TRADE_PROPOSAL`, `ORDER_FILLED`, `RISK_REJECT`, `SYSTEM_HALT`)
- `payload_json` (JSONB)
- `correlation_id` (UUID, for tracking a task through multiple agents)

**Memory Table Schema:**
- `agent_id` (FK)
- `lesson` (Text)
- `evidence_events` (List of Event IDs)
- `confidence` (Float 0-1)
- `created_at` / `last_confirmed` / `expires_at`

### 1.3 Communication Flow
1. **CEO** $\rightarrow$ `EventStore(TaskCreated)` $\rightarrow$ **QuantDev**.
2. **QuantDev** $\rightarrow$ `BrokerClient(GetHistoricalData)` $\rightarrow$ `EventStore(DataReceived)`.
3. **QuantDev** $\rightarrow$ `EventStore(StrategyProposal)`.
4. **ExecutionTrader** $\rightarrow$ `RiskGate(ValidateProposal)` $\rightarrow$ **Money-Zone**.
5. **Money-Zone** $\rightarrow$ `BrokerClient(PlaceOrder)` $\rightarrow$ `EventStore(OrderFilled)`.

---

## 2. Infrastructure Stack Proposal

For a transition from local Paper Trading to 24/7 VPS operation:

- **Language**: Python 3.11+ (Type-hinted, `asyncio`).
- **API Framework**: FastAPI (for the WebSocket Dashboard).
- **Database**: SQLite (WAL mode) for the Event Store and Memory.
- **Validation**: Pydantic v2 for all inter-zone messages (`TradeProposal`, `LimitCheck`).
- **Deployment**:
    - **Local**: `venv` + `.env` for development and initial Paper Trading.
    - **Production**: Docker Compose (Ubuntu VPS) containing:
        - `firm-core`: The Money-Zone and Agent Runtime.
        - `firm-api`: The FastAPI Dashboard server.
        - `sqlite-vol`: Persistent volume for the Event Store.
- **Monitoring**: Integrated telemetry in the Dashboard + structured logs.

---

## 3. Feasibility Analysis: The "200 CHF" Challenge

### 3.1 Constraint Audit
With a starting capital of $\sim 200$ CHF, the primary enemy is **friction** (Spreads, Commissions, Minimum Sizes).

| Factor | Impact | Risk |
|---|---|---|
| **Min Order Size** | If min size is too large, a single trade might exceed the 0.5% risk limit. | **High**: May force larger risk per trade than desired. |
| **Spreads** | Fixed or floating spreads can represent a significant % of the expected move on small timeframes. | **High**: Strategies must have a wide enough edge to cover the spread. |
| **Overnight Fees** | Holding positions across days (Swing Trading) will bleed the account. | **Medium**: Bias toward Intraday/Scalping. |
| **Taxes/Fees** | Any per-trade flat fee will kill a high-frequency strategy. | **Critical**: High-frequency is likely impossible. |

### 3.2 Risk Limit Validation
- **Daily Loss (2%)**: $\sim 4 \text{ CHF}$. Extremely tight. A few bad trades or a spread spike could trigger the halt.
- **Trade Risk (0.5%)**: $\sim 1 \text{ CHF}$. If the minimum order size requires a stop-loss that represents 5 CHF of risk, we cannot trade that instrument.
- **Drawdown Stop (8%)**: $\sim 16 \text{ CHF}$. Provides a small buffer but requires extreme discipline.

### 3.3 Cost Report Framework (Phase 1 Output)
To determine feasibility, the system will generate a report quantifying the following per instrument on the Whitelist:
$$\text{Friction Cost} = \text{Spread} + \text{Avg. Slippage} + \text{Overnight Fee (est.)}$$
$$\text{Net Edge} = \text{Gross Edge} - \text{Friction Cost}$$
If $\text{Net Edge} \le 0$, the instrument is automatically rejected. A result where no instruments pass is a valid "Stop" signal for the project.

---

## 4. Threat Model

- **The "Death by 1000 Cuts"**: The system stays within risk limits but slowly bleeds capital due to spreads.
- **API Latency Spike**: A stop-loss is triggered, but the API request is delayed, resulting in a loss greater than the 0.5% limit.
- **LLM "Over-Optimization"**: An agent finds a strategy that is "too perfect" on a 200 CHF scale but fails as soon as the account grows.
- **Zombie Process**: The Agent Runtime crashes, but the Money-Zone keeps a position open without a monitored stop-loss. (Mitigated by the **Reconciler** and **Server-side Stop-Loss**).

---

## 5. How Could This System Fail?
1. **Capital Insufficiency**: The 200 CHF starting capital may be below the "economic minimum" for the Capital.com fee structure.
2. **Risk Gate Bypass**: If a bug allows a `TradeProposal` to bypass the `RiskGate`. (Mitigated by making `BrokerClient` private to `ExecutionGateway`).
3. **Token Exhaustion**: The CEO spends the daily token budget on philosophy rather than research, leaving the firm blind.
4. **Data Quality**: The agents treat "Demo" data as "Live" without accounting for differences in execution quality.
