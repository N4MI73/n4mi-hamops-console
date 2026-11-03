"""How the console classifies an NWS product, the same in both modes (contract section 5).

`kind` comes from the event name: warning, watch, advisory, otherwise statement.

`hazard_class` (Dan, 2026-11-01): the hazard to the operating location decides
priority, not the product type. Checked in this order:
  1. temperature  -- heat, cold or wind chill (checked first, so "Wind Chill"
                     isn't mistaken for wind)
  2. site_hazard  -- tornado, thunderstorm, wind, flood, hurricane, tropical
                     storm, storm surge, or a Special Weather Statement
  3. other        -- everything else (Freeze Warning, Winter Storm Warning,
                     Red Flag Warning, Dense Fog Advisory, ...)

The word lists below are the one place to change; adding a hazard is a
one-line edit. Matching is on whole words, case-insensitive.
"""

import re

KINDS = ("warning", "watch", "advisory", "statement")
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}

TEMPERATURE_WORDS = ["heat", "cold", "wind chill"]
SITE_HAZARD_WORDS = ["tornado", "thunderstorm", "wind", "flood", "hurricane",
                     "tropical storm", "storm surge"]
SITE_HAZARD_EVENTS = ["special weather statement"]


def _has_word(text, words):
    return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)


def kind(event):
    text = (event or "").lower()
    for k in ("warning", "watch", "advisory"):
        if k in text:
            return k
    return "statement"


def hazard_class(event):
    text = (event or "").lower()
    if _has_word(text, TEMPERATURE_WORDS):
        return "temperature"
    if text.strip() in SITE_HAZARD_EVENTS or _has_word(text, SITE_HAZARD_WORDS):
        return "site_hazard"
    return "other"
