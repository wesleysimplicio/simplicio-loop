from types import SimpleNamespace


def os_with_name(os_module, name: str) -> SimpleNamespace:
    """Return a module-like OS proxy without mutating the process-global os module."""
    return SimpleNamespace(**{**vars(os_module), "name": name})
