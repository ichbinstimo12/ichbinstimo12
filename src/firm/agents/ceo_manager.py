import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum
import uuid
from src.firm.bus.event_store import EventStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("CEOManager")

class RequestStatus(Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"

@dataclass
class HiringRequest:
    request_id: str
    role: str
    justification: str
    config_proposal: Dict[str, Any]
    token_budget: int
    status: RequestStatus = RequestStatus.PENDING

class CEOManager:
    """
    Handles the management of the firm, specifically hiring and resource allocation.
    Ensures that no new agents or tool rights are added without user approval.
    """
    def __init__(self, event_store: EventStore):
        self.event_store = event_store
        self.hiring_requests: Dict[str, HiringRequest] = {}

    def create_hiring_request(self, role: str, justification: str, config: Dict[str, Any], budget: int) -> str:
        req_id = str(uuid.uuid4())
        request = HiringRequest(
            request_id=req_id,
            role=role,
            justification=justification,
            config_proposal=config,
            token_budget=budget
        )
        self.hiring_requests[req_id] = request

        # Log as an event for the Dashboard
        self.event_store.append("CEO", "HIRING_REQUEST", {
            "request_id": req_id,
            "role": role,
            "justification": justification,
            "budget": budget
        })

        logger.info(f"CEO created hiring request {req_id} for role {role}")
        return req_id

    def approve_hire(self, request_id: str):
        if request_id not in self.hiring_requests:
            raise ValueError("Request ID not found")

        self.hiring_requests[request_id].status = RequestStatus.APPROVED

        # Log approval
        self.event_store.append("USER", "HIRING_APPROVAL", {
            "request_id": request_id,
            "status": "APPROVED"
        })

        logger.info(f"Hiring request {request_id} APPROVED by user.")

    def reject_hire(self, request_id: str):
        if request_id not in self.hiring_requests:
            raise ValueError("Request ID not found")

        self.hiring_requests[request_id].status = RequestStatus.REJECTED

        # Log rejection
        self.event_store.append("USER", "HIRING_APPROVAL", {
            "request_id": request_id,
            "status": "REJECTED"
        })

        logger.info(f"Hiring request {request_id} REJECTED by user.")
