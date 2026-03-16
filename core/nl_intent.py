"""
Natural Language Intent Classification
Lightweight substring-based matching — no ML required.
"""

from __future__ import annotations

INTENT_REGISTRY = [
    {
        "intent": "dash",
        "examples": ["check temps", "show hardware stats", "how's the gpu", "system health", "dashboard"],
        "handler": "skill:dash",
    },
    {
        "intent": "shell",
        "examples": ["run a command", "execute", "run this"],
        "handler": "shell",
        "requires_arg": True,
    },
    {
        "intent": "file_read",
        "examples": ["read a file", "show me the file", "what's in", "open"],
        "handler": "file_read",
        "requires_arg": True,
    },
    {
        "intent": "fetch",
        "examples": ["fetch this url", "grab this page", "get this link"],
        "handler": "fetch",
        "requires_arg": True,
    },
    {
        "intent": "status",
        "examples": ["status", "are you up", "canopy status", "system status"],
        "handler": "skill:status",
    },
    {
        "intent": "weather",
        "examples": ["weather", "what's the weather", "is it raining", "temperature outside"],
        "handler": "skill:weather",
        "default_arg": "New York", 
    },
    {
        "intent": "exchange",
        "examples": ["exchange rate", "convert currency", "usd to eur", "how much is btc"],
        "handler": "skill:exchange",
    },
    {
        "intent": "python_shell",
        "examples": ["run python", "execute python", "python script", "run this code"],
        "handler": "skill:pyshell",
        "requires_arg": True,
    },
]


def classify_intent(message: str) -> dict | None:
    """
    Lightweight natural language intent classifier.

    Lowercases the message and checks each INTENT_REGISTRY entry for example
    substring matches. Scores by match length so more-specific phrases win.

    Returns:
        {
            "intent": str,
            "handler": str,
            "confidence": "high" | "medium",
            "arg": str | None,
        }
        or None if no match found.
    """
    lower = message.lower()

    # Collect all (entry, matched_example_length) pairs
    matched: list[tuple[dict, int]] = []

    for entry in INTENT_REGISTRY:
        best_len = 0
        for example in entry["examples"]:
            if example in lower:
                best_len = max(best_len, len(example))
        if best_len > 0:
            matched.append((entry, best_len))

    if not matched:
        return None

    # Sort by match length descending — longer example = more specific
    matched.sort(key=lambda x: x[1], reverse=True)

    if len(matched) == 1:
        confidence = "high"
        winner = matched[0][0]
    else:
        # Multiple intents matched — check if top two have the same longest score
        if matched[0][1] == matched[1][1]:
            confidence = "medium"
        else:
            # First match is unambiguously the best
            confidence = "high"
        winner = matched[0][0]

    # Try to extract an argument: everything after the matched example phrase
    arg: str | None = None
    for example in sorted(winner["examples"], key=len, reverse=True):
        idx = lower.find(example)
        if idx != -1:
            rest = message[idx + len(example):].strip(" :,")
            if rest:
                arg = rest
            break
    # Fall back to default_arg when the intent declares one and nothing was extracted
    if arg is None and winner.get("default_arg"):
        arg = winner["default_arg"]

    return {
        "intent": winner["intent"],
        "handler": winner["handler"],
        "confidence": confidence,
        "arg": arg,
        "matched_intents": [e["intent"] for e, _ in matched] if confidence == "medium" else None,
    }
