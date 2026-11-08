"""Line-link helpers shared by extraction and request persistence.

Operate on any objects exposing ``key``, ``parent_key`` and ``kind``
(``ExtractedLine``, ``OrderLineIn``).
"""

ACTIVE_EXCLUDED = ("alternative", "optional")
ADJUSTMENTS = ("discount", "surcharge", "shipping", "fee")


def active_keys(lines) -> set[str]:
    """Keys of active lines: not alternative/optional, and no inactive ancestor."""
    by_key = {ln.key: ln for ln in lines}

    def active(ln, seen: frozenset[str] = frozenset()) -> bool:
        if ln.kind in ACTIVE_EXCLUDED:
            return False
        if ln.parent_key is None or ln.key in seen or ln.parent_key not in by_key:
            return True
        return active(by_key[ln.parent_key], seen | {ln.key})

    return {ln.key for ln in lines if active(ln)}
