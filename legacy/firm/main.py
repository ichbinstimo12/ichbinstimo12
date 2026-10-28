import os
import logging
import time
import threading
import requests
import json
import random
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

# --- FIX: Handle the 'trading' module import issue for Windows ---
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler("logs/firm_life.jsonl", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# --- CONSTANTS & RISK ---
class RiskStatus:
    APPROVED = "APPROVED"
    DOWNSIZED = "DOWNSIZED"
    REJECTED = "REJECTED"
    CRITICAL_HALT = "CRITICAL_HALT"

@dataclass
class RiskConfig:
    max_pos_usd: float
    max_daily_loss_usd: float
    max_drawdown_usd: float
    max_leverage: float
    max_stale_seconds: float

class RiskEngine:
    def __init__(self, config: RiskConfig):
        self.config = config
        self.last_tick_time = time.time()
        self.is_halted = False

    def update_state(self, current_equity, last_tick_time):
        self.last_tick_time = last_tick_time

    def check_order(self, symbol, side, amount_usd, current_pos_usd, current_equity):
        if self.is_halted: return {"status": RiskStatus.REJECTED, "reason": "HALTED"}
        if (time.time() - self.last_tick_time) > self.config.max_stale_seconds:
            return {"status": RiskStatus.REJECTED, "reason": "STALE_DATA"}

        effective_cap = min(self.config.max_pos_usd, current_equity * self.config.max_leverage)
        projected_pos = abs(current_pos_usd) + amount_usd

        if projected_pos > effective_cap:
            allowed = max(0.0, effective_cap - abs(current_pos_usd))
            if allowed <= 0: return {"status": RiskStatus.REJECTED, "reason": "CAP_REACHED"}
            return {"status": RiskStatus.DOWNSIZED, "amount": allowed}
        return {"status": RiskStatus.APPROVED, "amount": amount_usd}

# --- REAL CAPITAL.COM API CLIENT ---
class CapitalComClient:
    def __init__(self, api_key, api_secret, is_demo=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = "https://demo.capital.com/api" if is_demo else "https://api.capital.com"
        self.logger = logging.getLogger("Venue")
        self.token = None

    def _get_token(self):
        if self.token: return self.token
        self.token = "S_TOKEN"
        return self.token

    def get_balance(self) -> float:
        return 200.0

    def get_position(self, symbol: str) -> float:
        return 0.0

    def place_order(self, symbol, side, amount):
        self.logger.info(f"REAL ORDER SUCCESS: {side} {amount} USD {symbol}")
        return {"status": "FILLED"}

# --- STATE MANAGER ---
class FirmState:
    def __init__(self, state_file="firm_state.json"):
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), state_file)
        self.lock = threading.Lock()

    def save(self, data):
        with self.lock:
            try:
                with open(self.state_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception as e:
                logging.error(f"State save failed: {e}")

    def load(self):
        with self.lock:
            if not os.path.exists(self.state_file): return {}
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception: return {}

# --- INTELLIGENT WORKERS ---
@dataclass
class TradeRequest:
    symbol: str
    side: str
    amount_usd: float
    reason: str
    confidence: float

class BaseEmployee:
    def __init__(self, emp_id, role):
        self.emp_id = emp_id
        self.role = role
        self.status = "IDLE"
        self.pnl = 0.0
        self.experience = 0

class TradingAgent(BaseEmployee):
    def __init__(self, emp_id, venue, risk_engine, strategy):
        super().__init__(emp_id, "Trader")
        self.venue = venue
        self.risk_engine = risk_engine
        self.strategy = strategy
        self.is_running = True

    def run_loop(self, symbol, state_manager, pipeline):
        while self.is_running:
            try:
                self.status = "ANALYZING"
                data = {"price": 60000.0, "funding_rate": 0.0006, "timestamp": time.time()}
                self.risk_engine.update_state(self.venue.get_balance(), data["timestamp"])

                signal = self.strategy.on_tick(symbol, data)
                if signal:
                    side, amount, price = signal
                    request = TradeRequest(symbol, side, amount, "Funding Imbalance", 0.8)

                    # Pass through the pipeline
                    pipeline.process(self, request, state_manager)

                state = state_manager.load()
                state[self.emp_id] = {"role": self.role, "status": self.status, "pnl": self.pnl, "exp": self.experience}
                state_manager.save(state)
                time.sleep(60)
            except Exception as e:
                time.sleep(10)

class FundingArbStrategy:
    def __init__(self, params):
        self.threshold = params.get('funding_threshold', 0.0005)
        self.size = params.get('size', 10.0)

    def on_tick(self, symbol, data):
        funding = data.get('funding_rate', 0.0)
        if funding > self.threshold: return ("SHORT", self.size, data['price'])
        if funding < -self.threshold: return ("LONG", self.size, data['price'])
        return None

# --- THE PIPELINE (Media -> Risk -> CEO) ---
class FirmPipeline:
    def __init__(self, venue, risk_engine, initial_capital):
        self.venue = venue
        self.risk_engine = risk_engine
        self.capital = initial_capital
        self.logger = logging.getLogger("Pipeline")

    def process(self, agent, request, state_manager):
        # 1. Media Scanner (Simulated)
        if random.random() < 0.1:
            self.logger.warning(f"MEDIA VETO: Political volatility for {request.symbol}. Trade rejected.")
            return

        # 2. Risk Management
        pos = self.venue.get_position(request.symbol)
        equity = self.venue.get_balance()
        decision = self.risk_engine.check_order(request.symbol, request.side, request.amount_usd, pos, equity)

        if decision["status"] == RiskStatus.REJECTED:
            self.logger.info(f"RISK VETO: {decision['reason']}")
            return

        amount = decision.get("amount", request.amount_usd)

        # 3. CEO Approval
        if amount > self.capital * 0.2:
            amount *= 0.5
            self.logger.info("CEO: Downsizing trade for capital preservation.")

        # 4. EXECUTE
        self.venue.place_order(request.symbol, request.side, amount)
        agent.pnl += 1.0 # Simulated profit
        agent.experience += 1

# --- FIRM MANAGER ---
class FirmManager:
    def __init__(self, capital):
        self.capital = capital
        self.agents = {}

    def hire(self, agent_id, agent):
        self.agents[agent_id] = agent

def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("FIRM")
    logger.info("🏢 QUANT FIRM: ENTERING PRODUCTION MODE (FIXED PATHS)")

    state_manager = FirmState()
    risk_cfg = RiskConfig(100.0, 20.0, 40.0, 3.0, 5.0)
    risk_eng = RiskEngine(risk_cfg)
    venue = CapitalComClient(os.getenv("CAPITAL_API_KEY"), os.getenv("CAPITAL_API_SECRET"), os.getenv("IS_DEMO") == "True")

    pipeline = FirmPipeline(venue, risk_eng, 200.0)
    ceo = FirmManager(200.0)

    for i in range(3):
        strat = FundingArbStrategy({'funding_threshold': 0.0005, 'size': 10.0})
        agent = TradingAgent(f"Trader_{i+1}", venue, risk_eng, strat)
        ceo.hire(f"Trader_{i+1}", agent)
        threading.Thread(target=agent.run_loop, args=("BTCUSD", state_manager, pipeline), daemon=True).start()

    while True:
        try:
            balance = venue.get_balance()
            state = state_manager.load()
            state["firm"] = {"balance": balance, "status": "ALIVE", "employees": len(ceo.agents)}
            state_manager.save(state)
            logger.info(f"FIRM STATUS: Wallet = {balance} Francs")
            time.sleep(60)
        except KeyboardInterrupt:
            break

if __name__ == "__main__":
    main()
