"""Compatibility import for the public HubQueueAgentClient surface."""

from .hub_queue_agent import (
    AGENT_SCHEMA,
    CAPABILITY,
    HubQueueAgentClient,
    HubQueueAgentError,
    HubQueueAgentJournal,
    HubQueueAgentUnavailable,
    IPC_SCHEMA,
)

__all__ = [
    "AGENT_SCHEMA", "CAPABILITY", "HubQueueAgentClient", "HubQueueAgentError",
    "HubQueueAgentJournal", "HubQueueAgentUnavailable", "IPC_SCHEMA",
]
