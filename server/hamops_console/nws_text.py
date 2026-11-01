"""Short, bounded alert text from NWS products: the "what" and "hazard" lines.

PORTED from Weather Sentinel (github.com/N4MI73/n4mi-weather-sentinel,
server/weather_sentinel_server.py, commit e2b4242, 2026-10-07): the
WHAT/HAZARD extraction behind its /api/conditions `what` and `hazard`
fields. A deliberate port (Ham Ops Console decision, 2026-11-01), so a
Field Mode alert read straight from api.weather.gov shows the same short
text a Home Mode alert gets from Weather Sentinel. Weather Sentinel owns
the original; if it changes there, decide deliberately whether to follow.

Changes from the original: the failure message goes to the logging module
instead of print(). The logic is otherwise unchanged.

Usage:
    what, hazard = extract_reason_fields(feature["properties"])

`props` is the `properties` object of one NWS alert (GeoJSON feature).
Never raises: this runs on untrusted external text, and an exception in a
poller would silently stop alert updates.
"""

import logging
import re

log = logging.getLogger(__name__)

WHAT_MAX_CHARS = 140
HAZARD_MAX_CHARS = 100

# Just the time phrase ("through 430 PM EDT", "until 8 PM EDT this evening"),
# wherever it sits -- whatever follows it (e.g. "FOR PORTIONS OF GEORGIA")
# is kept.
_TIME_PHRASE = re.compile(
    r"\s+(?:through|until|til|till)\s+\d{1,2}(?::?\d{2})?\s*[ap]\.?m\.?\s+[a-z]{3,4}\b"
    r"(?:\s+(?:this\s+)?(?:morning|afternoon|evening|night|tonight|today|sunday|monday|tuesday|wednesday|thursday|friday|saturday))?",
    re.IGNORECASE)
# Terminates a body paragraph at a blank line, at the next "* BULLET", or
# at the next ALL-CAPS label such as "WHERE..." (case-sensitive on purpose,
# so ordinary sentences like "Locations impacted include..." never match).
_PARA_END = r"(?:\n[ \t]*\n|\n[ \t]*\*|\n[ \t]*(?-i:[A-Z][A-Z ]{2,20}\.\.\.)|\Z)"
_HAZARD_RE = re.compile(r"^[ \t]*\*?[ \t]*HAZARD\.\.\.(.*?)" + _PARA_END, re.IGNORECASE | re.MULTILINE | re.DOTALL)
_WHAT_RE = re.compile(r"^[ \t]*\*?[ \t]*WHAT\.\.\.(.*?)" + _PARA_END, re.IGNORECASE | re.MULTILINE | re.DOTALL)


def _squash(text):
    """Collapse every run of whitespace (including the hard line breaks
    NWS inserts at ~69 columns) to a single space."""
    return re.sub(r"\s+", " ", text).strip() if isinstance(text, str) else ""


def _bound(text, limit):
    """Trim to `limit` characters at a word boundary, marking the cut."""
    text = _squash(text)
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    space = cut.rfind(" ")
    if space > limit * 0.5:
        cut = cut[:space]
    return cut.rstrip(" ,;:-") + "..."


def extract_reason_fields(props):
    """Returns (what, hazard) -- either may be None. Never raises."""
    try:
        event = _squash(props.get("event"))
        description = props.get("description")
        description = description if isinstance(description, str) else ""

        # HAZARD: "HAZARD..." (warnings, statements) or "* WHAT..." (watches,
        # advisories, and NWS's newer plain-language format).
        hazard = None
        for pattern in (_HAZARD_RE, _WHAT_RE):
            m = pattern.search(description)
            if m:
                hazard = _bound(m.group(1), HAZARD_MAX_CHARS) or None
                break

        # WHAT: NWS's own headline, without its trailing time phrase (the
        # device already shows "Until ...").
        params = props.get("parameters")
        raw_headline = None
        if isinstance(params, dict):
            values = params.get("NWSheadline")
            if isinstance(values, list) and values and isinstance(values[0], str):
                raw_headline = values[0]
        what = _squash(raw_headline)
        what = re.sub(r"^\.{2,}\s*|\s*\.{2,}$", "", what).strip()
        stripped = _TIME_PHRASE.sub("", what, count=1).strip()
        if len(stripped) >= 12:
            what = stripped

        # No headline at all: fall back to the product's first paragraph,
        # unless that paragraph is itself a labelled section (which is what
        # the HAZARD slot already carries).
        if not what and description.strip():
            first_par = re.split(r"\n[ \t]*\n", description.strip(), maxsplit=1)[0]
            if not re.match(r"^\s*\*?\s*(HAZARD|WHAT|WHERE|WHEN|IMPACTS?)\.\.\.", first_par, re.IGNORECASE):
                what = _squash(first_par)

        what = _bound(what, WHAT_MAX_CHARS)

        # A headline that merely repeats the event name ("Heat Advisory
        # remains in effect") adds nothing next to the event line.
        if what and event and what.lower().startswith(event.lower()) and len(what) <= len(event) + 25:
            what = ""
        if what and hazard and what.lower() == hazard.lower():
            what = ""

        return (what or None), hazard
    except Exception as e:  # noqa: BLE001 -- see docstring
        log.warning("could not extract reason fields: %r", e)
        return None, None
