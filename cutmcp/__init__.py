"""cutmcp — raw interview footage to a cut timeline, in three tiers.

    tier 1  extract.py    deterministic   transcript, pauses, speakers, loudness
    tier 2  decide.py     probabilistic   typed Jev questions, batched
    tier 3  assemble.py   deterministic   knapsack DP, review queue, EDL emit

Tiers 1 and 3 never call a model. Tier 2 never measures anything. Identical
scores therefore produce an identical timeline, byte for byte.
"""

__version__ = "0.1.0"

__all__ = ["jev", "extract", "decide", "assemble"]
