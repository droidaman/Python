"""Exceptions shared by the engine and the broker adapters."""


class HaltError(RuntimeError):
    """A safety check failed; no further orders are sent in this run."""
