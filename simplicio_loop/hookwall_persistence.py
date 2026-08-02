"""Hookwall persistence compatibility facade backed by MapperStore.

The legacy local SQLite ledger is intentionally no longer a production
writer.  ``MapperHookwallEffectLedger`` owns effect durability and causal
events in the canonical operations store.
"""
from __future__ import annotations

from .mapper_hookwall import MapperHookwallEffectLedger


class HookwallEffectLedger(MapperHookwallEffectLedger):
    """Compatibility name that refuses to recreate a legacy ledger."""

    def __init__(self, database, *, operations=None, auto_create=False):
        if operations is None:
            raise RuntimeError("LEGACY_HOOKWALL_READ_ONLY")
        super().__init__(database, operations=operations, auto_create=auto_create)

__all__ = ["HookwallEffectLedger", "MapperHookwallEffectLedger"]
