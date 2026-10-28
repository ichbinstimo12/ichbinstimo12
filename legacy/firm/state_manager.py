import json
import os
import threading
import logging

class FirmState:
    """
    Thread-safe state manager to prevent JSONDecodeError (Race Conditions).
    Ensures that only one agent can write to the firm_state.json at a time.
    """
    def __init__(self, state_file="firm_state.json"):
        # Use absolute path to avoid directory confusion
        self.state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), state_file)
        self.lock = threading.Lock()
        self.logger = logging.getLogger("StateMgr")

    def save(self, data):
        try:
            with self.lock:
                with open(self.state_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
        except Exception as e:
            self.logger.error(f"Failed to save state: {e}")

    def load(self):
        try:
            with self.lock:
                if not os.path.exists(self.state_file):
                    return {}
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            self.logger.error(f"Failed to load state: {e}")
            return {}
