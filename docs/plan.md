# Implementation Plan: Autonomous Trading Firm

## 1. Build Strategy
We follow a **Test-First, Modular approach**. No module is considered complete until its specific failure scenarios are tested and verified. We build from the "Bottom-Up" (Infrastructure $\rightarrow$ Logic $\rightarrow$ Agents $\rightarrow$ UI).

### Core Development Principles:
- **Zero-Trust Money-Zone**: Every interaction with the `BrokerClient` must pass through the `RiskGate`.
- **Event-Driven**: All state changes are captured as immutable events.
- **Fail-Fast**: System Halt occurs only on: Account Limit breaches, Reconciler mismatches, or critical Data Outages. Normal `RiskGate` rejections of `TradeProposals` are logged but do not halt the system.
- **Isolation**: Strict separation of concerns via containers and limited access rights.

---

## 2. Modular Roadmap

### Module 1: The Foundation (Event Store & Infrastructure)
*Goal: Establish the immutable audit trail and isolated environment.*
- **Tasks**:
    - SQLite database initialization (WAL mode) for Event Store and Memory.
    - Event Store implementation (Append-only).
    - **Isolation**: Container setup (FastAPI API, Firm-Core, Separate Read-Only Data Service).
    - Basic Pub/Sub mechanism for internal communication.
- **Tests**:
    - **Immutability Test**: Verify that once an event is written, it cannot be modified/deleted via API.
    - Concurrent write stress test.
    - Event retrieval and correlation ID filtering.

### Module 2: Broker Connectivity (Read-Only)
*Goal: Secure access to Capital.com Demo data.*
- **Tasks**:
    - `BrokerClient` REST implementation (Session, Tokens).
    - Market data loaders (Historical prices with strict range/gap limits).
    - WebSocket integration for real-time price streaming.
- **Tests**:
    - Token refresh cycle verification.
    - Data integrity check (No gaps/future leaks).
    - Rate limit handling.

### MODULE 2.5: THE FEASIBILITY GATE (Cost Report)
*Goal: Quantitative Go/No-Go decision.*
- **Tasks**:
    - Generate report on Spreads, Slippage, and Overnight Fees per instrument.
    - Calculate $\text{Net Edge}$ vs. the 0.5% risk limit.
- **GATE**: If no instruments are found to be economically viable at the $\sim 200$ CHF scale, the project stops or limits are renegotiated here.

### Module 3: The Money-Zone Firewall (Risk & Kill)
*Goal: Absolute capital protection.*
- **Tasks**:
    - `limits.toml` parser (2% Daily, 0.5% Trade, 8% DD).
    - `RiskGate` implementation (Limit checks, Whitelist validation).
    - `KillSwitch` (Flatten all + Session refresh loop).
    - **Reconciler**: Logic to block new orders $\rightarrow$ Multiple retries $\rightarrow$ System Halt if mismatch persists.
- **Tests**:
    - **Failure Injection**: Simulate token expiry during `KillSwitch`.
    - **Boundary Testing**: Trigger account limits.
    - **Reconciliation**: Simulate Broker position change; verify order block $\rightarrow$ halt.
- **Deliverable**: Minimal "Live Window" (Telemetry feed) to verify Money-Zone health.

### Module 4: Execution Gateway (Demo Only)
*Goal: Safe order placement against Demo account.*
- **Tasks**:
    - `TradeProposal` Pydantic models.
    - Order lifecycle (Proposal $\rightarrow$ Order $\rightarrow$ Fill).
    - Server-side Stop-Loss enforcement.
    - **Modi-Leiter**: Implementation of `approve-live` token check.
- **GATE**: First actual Demo order only after manual verification of the Execution Gateway logic.

### Module 5: Research & Backtest Engine
*Goal: Evidence-based strategy discovery.*
- **Tasks**:
    - Backtest runner with realistic friction.
    - **Hold-out Discipline**: Strict temporal split (Train/Val/Hold-out).
    - Trial-Counter implementation.
- **Tests**:
    - Look-ahead Bias Test.
    - Cost-Sensitivity Test.

### Module 6: Agent Runtime & Memory
*Goal: The "Brain" of the firm.*
- **Tasks**:
    - Role-based Agent loader (YAML).
    - LLM Loop (Observe $\rightarrow$ Think $\rightarrow$ Act).
    - Memory: Episodic and Semantic (Lessons).
    - **Hiring & Tooling**: User-approval flow for new roles/rights.
- **Tests**:
    - **Prompt Injection Tests**: Attempt to bypass `RiskGate` via news data.
    - **Replay Tests**: Run Agent loop against recorded LLM responses for deterministic testing.
    - Token budget enforcement.

### Module 7: API & Dashboard
*Goal: Real-time observability.*
- **Tasks**:
    - FastAPI WebSocket server.
    - `web/office.html` isometric view.
    - CEO Command Center (Hiring approval, Mode toggle).
- **Tests**:
    - WebSocket latency check.
    - `OFFLINE` state trigger.

---

## 3. Operational Tools & Validation

- **Paper-Runner**: Tool to run agents against Demo, generating automatic daily reports (PnL, Drawdown, Token costs).
- **Runbook**: Documentation for Start, Stop, Kill, and Recovery procedures.
- **Total Failure Injection**: Comprehensive suite testing (Broker Down + Token Expired + VPS Crash).

## 4. Sequence of Execution
1. **Foundation** $\rightarrow$ 2. **Read-Only Broker** $\rightarrow$ **(FEASIBILITY GATE)** $\rightarrow$ 3. **Risk/Kill/Reconcile** $\rightarrow$ 4. **Execution** $\rightarrow$ **(ORDER GATE)** $\rightarrow$ 5. **Research** $\rightarrow$ 6. **Agents** $\rightarrow$ 7. **Dashboard**.

---

## 5. How Could This Plan Fail?
- **Economic Dead-End**: The Cost Report reveals that Capital.com's fees make the 200 CHF account a "leaking bucket".
- **API Instability**: Frequent session drops making the Reconciler too sensitive.
- **Agent Drift**: Agents finding "optimal" strategies that are actually just overfitting to the Demo environment.
- **Container Latency**: Overhead in the isolated data service affecting real-time fills.
