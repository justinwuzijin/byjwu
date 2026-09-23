"""Errors an editor can act on. These are usage and contract failures, not bugs."""


class ConductorError(RuntimeError):
    """Raised for a bad input, a refused write, or a Jev call that cannot proceed."""
