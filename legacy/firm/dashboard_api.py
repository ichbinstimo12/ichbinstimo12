import os
import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv
import json

# --- API Models ---
class FirmStatusResponse(BaseModel):
    total_equity: float
    active_workers: List[str]
    performance: Dict[str, Any]

class ControlRequest(BaseModel):
    command: str # "REBALANCE" or "HALT"
    target_agent: Optional[str] = None

# --- FastAPI Server ---
app = FastAPI(title="Quant Firm Command Center")

# CRITICAL FIX: Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()

def load_firm_state():
    """Reads the shared state file updated by main.py using absolute paths."""
    state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firm_state.json")
    if not os.path.exists(state_file):
        return {"firm": {"balance": 0.0, "status": "OFFLINE"}, "workers": {}}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"firm": {"balance": 0.0, "status": "OFFLINE"}, "workers": {}}

@app.get("/status", response_model=FirmStatusResponse)
async def get_status():
    """Returns the current state of the firm by reading the state file."""
    try:
        state = load_firm_state()

        # Ensure state is a dictionary
        if not isinstance(state, dict):
            state = {"firm": {"balance": 0.0, "status": "OFFLINE"}, "workers": {}}

        # Extract worker IDs (excluding the 'firm' key)
        workers = [k for k in state.keys() if k != "firm"]

        # Format performance data
        performance = {}
        for w in workers:
            performance[w] = state[w]

        return FirmStatusResponse(
            total_equity=float(state.get("firm", {}).get("balance", 0.0)),
            active_workers=workers,
            performance=performance
        )
    except Exception as e:
        # Return a safe default instead of a 500 error
        return FirmStatusResponse(
            total_equity=0.0,
            active_workers=[],
            performance={}
        )

@app.post("/control")
async def send_command(req: ControlRequest):
    """Allows the owner to send commands to the CEO."""
    try:
        cmd_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firm_commands.txt")
        with open(cmd_file, "a", encoding="utf-8") as f:
            f.write(f"{req.command}:{req.target_agent}\n")
        return {"status": "Success", "message": f"Command {req.command} sent to CEO."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
