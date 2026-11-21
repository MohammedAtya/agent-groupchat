"""All available agent adapters, keyed by id."""
from .base import Adapter, RunContext
from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS = {a.id: a for a in (ClaudeAdapter(), CodexAdapter())}

__all__ = ["ADAPTERS", "Adapter", "RunContext"]
