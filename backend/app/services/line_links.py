"""Line-link helpers shared by extraction and request persistence.

Operate on any objects exposing ``key``, ``parent_key`` and ``kind``
(``ExtractedLine``, ``OrderLineIn``); the group helpers also read
``commodity_group_id`` and ``total_price``.
"""

ACTIVE_EXCLUDED = ("alternative", "optional")
ADJUSTMENTS = ("discount", "surcharge", "shipping", "fee")


def parent_allowed(kind: str, parent_kind: str) -> bool:
    """Whether a line of ``kind`` may point at a parent of ``parent_kind``.

    The one definition of the by-kind parent rule: an item has no parent;
    alternative and optional need an item; discount, surcharge, shipping and fee
    may attach to an item, alternative or optional (never to another adjustment).
    """
    if kind == "item":
        return False
    if kind in ACTIVE_EXCLUDED:
        return parent_kind == "item"
    return parent_kind not in ADJUSTMENTS


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


def inherited_groups(lines, *, override: bool) -> list[int | None]:
    """Per-line commodity group ids after adjustment inheritance.

    A discount, surcharge, shipping or fee line with a resolved parent takes its
    parent's group: always (``override=True``, extraction) or only when it has
    none of its own (``override=False``, saving a request).
    """
    by_key = {ln.key: ln for ln in lines}
    out: list[int | None] = []
    for ln in lines:
        gid = ln.commodity_group_id
        parent = by_key.get(ln.parent_key) if ln.parent_key is not None else None
        if ln.kind in ADJUSTMENTS and parent is not None and parent is not ln:
            if override or gid is None:
                gid = parent.commodity_group_id
        out.append(gid)
    return out


def primary_group(lines) -> int | None:
    """The group with the largest summed total over the active lines.

    Lines without a group are ignored; ties go to the group whose first active
    line comes earliest. None when no active line has a group.
    """
    keys = active_keys(lines)
    sums: dict[int, float] = {}
    first: dict[int, int] = {}
    for index, ln in enumerate(lines):
        gid = ln.commodity_group_id
        if gid is None or ln.key not in keys:
            continue
        sums[gid] = sums.get(gid, 0.0) + (ln.total_price or 0.0)
        first.setdefault(gid, index)
    if not sums:
        return None
    return max(sums, key=lambda g: (round(sums[g], 2), -first[g]))
