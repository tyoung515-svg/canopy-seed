"""
Canopy Seed shared exception types.

Kept in a separate module to avoid circular imports between
agent_pool.py and ai_backend.py (each imports from the other).
"""


class ToolExhaustionError(RuntimeError):
    """Raised when _gemini_complete's tool loop exhausts MAX_TOOL_ROUNDS.

    Distinct from a transient API/network failure: exhaustion means the model
    DID work (wrote files, ran tests) but never called declare_done.  Callers
    should accept partial results and rerun tests rather than retrying the same
    prompt on another backend (which would re-exhaust identically).
    """
    pass
