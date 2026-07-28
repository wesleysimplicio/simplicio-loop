"""Lightweight public facade for the Simplicio Loop command surface.

The implementation lives in ``cli_impl`` so agents and token-budget gates can
inspect this stable entrypoint without loading a thousand-line parser. Public
attributes remain compatible, including pytest monkeypatch propagation.
"""

from __future__ import annotations

import sys
import types

from . import cli_impl as _impl

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


class _CliFacade(types.ModuleType):
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name != "_impl" and hasattr(_impl, name):
            setattr(_impl, name, value)


sys.modules[__name__].__class__ = _CliFacade


if __name__ == "__main__":
    raise SystemExit(_impl.main())
