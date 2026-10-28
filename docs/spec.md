# Specification: Autonomous Trading Firm (Agent-Based)

## 1. Goals & Objectives
The goal is to build a fully autonomous trading firm powered by specialized LLM agents, operating on **Capital.com**. The system is designed to move from theoretical research to live execution through a rigorous, multi-stage validation process (AgenKit), where survival of capital is the primary metric of success.

### Primary Objectives:
- **Autonomous Operation**: Agents handle research, strategy implementation, and trade proposals.
- **Deterministic Execution**: A "Money-Zone" ensures that no LLM ever directly touches funds or overrides hard risk limits.
- **Transparent Reasoning**: Every action, thought, and tool-call is logged to an immutable event store.
- **Evidence-Based Learning**: Agents maintain a memory of "Lessons" backed by specific event IDs.
- **Risk-First Architecture**: Hard-coded limits, a global Kill-Switch, and a Reconciler ensure system integrity.

### Non-Goals:
- **Direct Fund Management**: The system will not handle deposits, withdrawals, or transfers.
- **Guaranteed Profits**: The system is not designed to "promise" returns, but to identify and execute a sustainable edge if one exists.
- **General Purpose AI**: The agents are specialized for trading and firm management, not general tasks.

---

## 2. Roles & Responsibilities

### Brain-Zone (Agents)
Managed via `config/roles/*.yaml`.
- **CEO**: Strategic lead, resource allocation, hiring/firing, task prioritization. **Requires user approval for Hiring and new Tool Rights**.
- **Head of Research**: Hypothesis generation, validation management, Trial-Counter oversight.
- **Quant Developer**: Strategy implementation (Signal modules), backtest engineering.
- **Data Engineer**: Data pipeline integrity, quality checks, look-ahead prevention.
- **Macro/News Analyst**: Regime detection, event flagging with source citations.
- **Risk Officer**: Monitoring KPIs, proposing limit tightenings.
- **Execution Trader**: Translating signals to `TradeProposal`, monitoring slippage/fills.
- **Red Team / Auditor**: Adversarial strategy testing, defining Kill-Criteria.

### Money-Zone (Deterministic Code)
- **RiskGate**: The final validator. Validates every `TradeProposal` against `limits.toml` and real-time account data.
- **ExecutionGateway**: Communicates with `BrokerClient`, manages order idempotency.
- **BrokerClient**: Low-level API wrapper for Capital.com (REST + WS).
- **KillSwitch**: Emergency flatten-all and agent-stop mechanism. Must handle token expiration and implement aggressive retries until Broker confirms "flat".
- **Reconciler**: Ensures internal state matches Broker truth every few seconds.

---

## 3. Hard Limits & Constraints
Stored in `config/limits.toml` (Read-Only for agents).

### Core Risk Limits:
- **Daily Loss Limit**: Hard stop at **2%** of account value.
- **Trade Risk**: Maximum **0.5%** of account value per trade.
- **Max Drawdown**: Hard stop at **8%** from equity peak.
- **Note**: These limits must be validated against real minimum order sizes during Phase 1.

### Operational Limits:
- **Max Position Size**: Per instrument and aggregate.
- **Max Leverage**: Absolute cap on account leverage.
- **Token Budget**: Daily caps per agent and for the whole firm.
- **Hiring Cap**: Max number of agents and new hires per week.

---

## 4. Operational Modes & Security

### Modi-Leiter (The Ladder)
Movement is only permitted upwards with explicit user approval:
`BACKTEST` $\rightarrow$ `PAPER (Demo)` $\rightarrow$ `LIVE_MICRO` $\rightarrow$ `LIVE`.
- **Approval**: Switching to any `LIVE_*` mode requires a time-limited, account-bound approval token generated via CLI command `firm approve-live`. Without this token, the Execution Gateway rejects live orders.

### Market Access
- **Scanning**: Agents may scan all available markets (read-only).
- **Trading**: Trading is permitted **only** for instruments on a manually approved Whitelist. An instrument enters the Whitelist only after passing spread, cost, and data filters and receiving user approval.

### Secrets & Data Integrity
- **Secrets**: Stored exclusively in `.env` or environment variables. Never logged, never displayed in Dashboard, never passed to Agent prompts.
- **Truth**: No fabricated numbers. Every value in the Dashboard or Report must originate from real data/calculations. Missing data is shown as `—` or `OFFLINE`.

---

## 5. Live Office Dashboard (`web/office.html`)
A real-time isometric/top-down view of the firm, synced via WebSocket to the Event Store.

### Key Features:
- **Live Office View**: Visual representation of agents at desks. Animations reflect state (thinking, tool-calling, error, paused).
- **Agent Detail Panel**: 
    - Live stream of observations, reasoning, tool-calls, and results.
    - Task history and reporting line.
    - Memory: Lessons (with evidence links) and Playbook version.
    - Performance metrics (Quality, Error rate, Token cost).
- **CEO Command Center**: Task list, bottleneck metrics, Hiring Requests with "Approve/Reject" buttons.
- **Strategy Wall**: Table of candidates with Verdict, Trial-Counter, and Live Status.
- **Global Header**: Current Mode (color-coded), Broker Balance, Daily PnL, Drawdown, Connectivity Status, and a prominent **KILL-SWITCH** button (with confirmation).
- **System State**: If backend is unreachable, display a large `OFFLINE` warning.

### Acceptance Criteria:
- Zero delay between Event Store entry and Dashboard update.
- Kill-Switch triggers immediate flatten-all process.
- Detail panels show raw logs, not summarized versions.

---

## 6. Acceptance Criteria & Build Gates

- **Phase 0 (Spec)**: Documented goals, roles, and constraints.
- **Phase 1 (Arch & Feasibility)**: Technical blueprint and a **Cost Report**. This report must quantify spreads, commissions, and overnight fees per instrument. A negative result (i.e., "trading is not profitable at this scale") is a valid outcome and may lead to a project stop or limit adjustment.
- **Phase 2 (Plan)**: Modular build sequence with test-first definitions.
- **Phase 3 (Build)**: 
    - Event-bus functional.
    - BrokerClient reads Demo account successfully.
    - RiskGate rejects orders exceeding `limits.toml`.
    - KillSwitch flattens positions and handles token expiry.
- **Phase 4 (Review)**: Red Team identifies 3+ failure scenarios; all are mitigated.
- **Phase 5 (Paper)**: Demo trading for $\ge 4$ weeks. Must meet pre-defined tolerances for PnL vs Backtest and a minimum number of trades to be statistically relevant.
- **Phase 6 (Live-Micro)**: Smallest possible orders. Requires strict alignment between internal state and Broker state, and a hard slippage limit for order acceptance.

---

## 7. Assumptions
- **API Access**: Assumes official Capital.com API is available and stable.
- **Execution Variance**: **Assumption**: Demo execution (fills, slippage, speed) $\neq$ Live execution.
- **Compute**: Assumes a VPS capable of running Docker-Compose with Python 3.11+.
- **Latency**: Assumes standard REST/WS latency is acceptable.
- **Capital**: Initial capital is $\sim 200$ CHF.

---

## 8. How Could This System Fail? (Initial Analysis)
- **Cost Erosion**: Spreads and overnight fees on a small account could eat any edge generated by the agents.
- **LLM Hallucination**: An agent might misinterpret a news event or a data point, leading to a flawed `TradeProposal` (mitigated by `RiskGate` and `Red Team`).
- **API Fragility**: Token expiration or rate-limiting during a high-volatility event could prevent the `KillSwitch` from executing.
- **Overfitting**: Agents might "discover" a strategy that is "too perfect" on a 200 CHF scale but fails as soon as the account grows (mitigated by Trial-Counter and Red Team).
- **Prompt Injection**: External news data fed to the Macro Analyst could contain instructions to bypass rules (mitigated by deterministic Money-Zone).
