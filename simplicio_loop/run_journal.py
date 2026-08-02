"""RunJournal compatibility facade backed by MapperStore.

Loop no longer owns a journal database.  The public name remains available to
callers while the durable implementation lives in ``mapper_run_journal`` and
the canonical MapperStore operations database.
"""
from __future__ import annotations

from .mapper_run_journal import (
    EVENT_SCHEMA,
    GENESIS_HASH,
    TERMINAL_SCHEMA,
    MapperJournalError,
    MapperRunJournal,
)

JournalError = MapperJournalError
JournalIntegrityError = MapperJournalError


class RunJournal(MapperRunJournal):
    """Compatibility name that refuses to recreate a legacy journal."""

    def __init__(self, database, *, adapter=None, auto_create=False):
        if adapter is None:
            raise JournalIntegrityError("LEGACY_JOURNAL_READ_ONLY")
        super().__init__(database, adapter=adapter, auto_create=auto_create)

__all__ = [
    "EVENT_SCHEMA",
    "GENESIS_HASH",
    "JournalError",
    "JournalIntegrityError",
    "MapperJournalError",
    "RunJournal",
    "TERMINAL_SCHEMA",
]
