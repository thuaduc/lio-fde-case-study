"""Eval: PDF quote extraction, classification and catalog suggestions.

Scores ``extract_from_pdf`` on the five real Acme vendor quotes against the
expected values below, runs classification checks on synthetic quote text, and
checks catalog suggestions. Uses the live OpenAI key (``OPENAI_API_KEY``).

    cd backend
    set -a; . ../.env; set +a
    .venv/bin/python -m scripts.eval_extraction \
        [--quotes-dir DIR] [--models gpt-4o-mini,gpt-6.1-sol] [--baseline] [--skip-catalog]

It is a report: the exit code is always 0. Quote PDFs are read from QUOTES_DIR
(default ``../../quotes`` from ``backend/``) and are never copied into the repo.
"""
from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

MONEY_TOL = 0.01


# --------------------------------------------------------------------------
# Pure scoring helpers (unit-tested in tests/test_eval_scorer.py)
# --------------------------------------------------------------------------


def vendor_ok(actual: str | None, needles: tuple[str, ...]) -> bool:
    """Case-insensitive substring match against any accepted needle."""
    text = (actual or "").lower()
    return any(n.lower() in text for n in needles)


def tax_id_ok(actual: str | None, expected: str | None) -> bool:
    """Digits-only compare; an expected None means the quote has no tax ID."""
    digits = re.sub(r"\D", "", actual or "")
    return digits == (expected or "")


def money_ok(actual: float | None, expected: float) -> bool:
    return actual is not None and abs(actual - expected) <= MONEY_TOL + 1e-9


def totals_ok(actual: list[float], expected: list[float]) -> bool:
    """Same multiset of line totals, each within a cent."""
    if len(actual) != len(expected):
        return False
    remaining = sorted(actual)
    for e in sorted(expected):
        hit = next((a for a in remaining if abs(a - e) <= MONEY_TOL + 1e-9), None)
        if hit is None:
            return False
        remaining.remove(hit)
    return True


# --------------------------------------------------------------------------
# Expected values
# --------------------------------------------------------------------------


@dataclass
class Expected:
    file: str  # file name or glob (matched case-insensitively)
    short: str
    vendor: tuple[str, ...]
    tax_id: str | None
    active: list[float]
    net: float
    tax: float
    grand: float
    alternatives: list[float] = field(default_factory=list)
    alt_parent_total: float | None = None  # total of the item the alternatives replace
    parentless_shipping: bool = False  # shipping lines must have parent null
    warning_contains: str | None = None


EXPECTED = [
    Expected("QT-4120-Cust-14918.pdf", "greenmantle", ("greenmantle",), "853252405",
             [715.26, 622.00, 215.00], 1552.26, 133.88, 1686.14,
             alternatives=[430.00], alt_parent_total=622.00, parentless_shipping=True),
    Expected("QT-OF2312380-Cust-57692.pdf", "verdeform", ("verdeform", "bloomcraft"), None,
             [716.56, 120.49, 350.00, 113.85], 1300.90, 112.20, 1413.10,
             warning_contains="fuel surcharge"),
    Expected("Quote_1__*2214703918.pdf", "apple", ("apple",), "942404110",
             [1409.06, 4.00], 1413.06, 121.53, 1534.59),
    Expected("Quote_Hartwell_*Machine.pdf", "hartwell", ("hartwell",), None,
             [15000.00, 2000.00], 17000.00, 1402.50, 18402.50),
    Expected("QuoteA0492_23.Pdf", "hollis", ("hollis",), "941985704",
             [1560.00, 320.00], 1880.00, 162.15, 2042.15,
             alternatives=[2090.00, 1830.00], alt_parent_total=1560.00,
             parentless_shipping=True),
]

# (text, expected group name or None, group names that are forbidden)
CLASSIFICATION_CASES = [
    ("Cable ties 8 in, black, pack of 100", "Electrical Engineering", ()),
    ("HP 58A black toner cartridge", "IT Hardware", ()),
    ("Notion Plus plan, monthly subscription, 5 seats", "Office Supplies", ()),
    ("Dell Latitude 7450 business laptop", None, ("IT Hardware", "Office Supplies")),
    ("A4 copy paper, 80 gsm, 5 reams", "Copy Paper", ("Office Supplies",)),
]


def find_quote(quotes_dir: Path, pattern: str) -> Path | None:
    lowered = pattern.lower()
    for p in sorted(quotes_dir.iterdir()):
        if p.is_file() and _glob_match(p.name.lower(), lowered):
            return p
    return None


def _glob_match(name: str, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(name, pattern)


# --------------------------------------------------------------------------
# Scoring one extraction
# --------------------------------------------------------------------------

Check = tuple[str, bool, str]  # (field, passed, "actual vs expected" detail)


def score_extraction(exp: Expected, resp) -> list[Check]:
    from app.services.line_links import active_keys

    checks: list[Check] = []
    if not resp.success or resp.data is None:
        return [("extraction", False, f"failed: {resp.error}")]
    d = resp.data
    lines = d.order_lines
    keys = active_keys(lines)
    active = [ln for ln in lines if ln.key in keys]
    alts = [ln for ln in lines if ln.kind == "alternative"]

    checks.append(("vendor", vendor_ok(d.vendor_name, exp.vendor),
                   f"actual {d.vendor_name!r} vs expected one of {exp.vendor}"))
    checks.append(("tax_id", tax_id_ok(d.vat_id, exp.tax_id),
                   f"actual {d.vat_id!r} vs expected {exp.tax_id!r}"))
    a_tot = [ln.total_price for ln in active]
    checks.append(("active_lines", totals_ok(a_tot, exp.active),
                   f"actual {sorted(a_tot)} vs expected {sorted(exp.active)}"))
    if exp.alternatives or alts:
        t = [ln.total_price for ln in alts]
        checks.append(("alternatives", totals_ok(t, exp.alternatives),
                       f"actual {sorted(t)} vs expected {sorted(exp.alternatives)}"))
    if exp.alt_parent_total is not None:
        parent_keys = {ln.key for ln in lines
                       if ln.kind == "item" and money_ok(ln.total_price, exp.alt_parent_total)}
        bad = [(ln.key, ln.total_price, ln.parent_key) for ln in alts
               if ln.parent_key not in parent_keys]
        checks.append(("alt_parent", bool(alts) and not bad,
                       f"alternatives not parented to the {exp.alt_parent_total} line: {bad}"
                       f" (parent keys {sorted(parent_keys)})"))
    if exp.parentless_shipping:
        ship = [ln for ln in active if ln.kind == "shipping"]
        bad = [(ln.key, ln.parent_key) for ln in ship if ln.parent_key is not None]
        checks.append(("shipping_parent_null", bool(ship) and not bad,
                       f"shipping lines {[(ln.key, ln.parent_key) for ln in ship]} vs expected parent null"))
    checks.append(("net", money_ok(d.net_total, exp.net), f"actual {d.net_total} vs expected {exp.net}"))
    checks.append(("tax", money_ok(d.tax_total, exp.tax), f"actual {d.tax_total} vs expected {exp.tax}"))
    checks.append(("grand", money_ok(d.total_cost, exp.grand), f"actual {d.total_cost} vs expected {exp.grand}"))

    warnings = resp.warnings or []
    unverified = {m.group(1) for w in warnings if (m := re.match(r"Couldn't find line (\S+)'s price", w))}
    no_box = [ln.key for ln in active if not ln.sources]
    unver = [ln.key for ln in active if ln.key in unverified]
    checks.append(("boxes_verified", not no_box and not unver,
                   f"active lines without boxes {no_box}; unverified price {unver}"))
    if exp.warning_contains:
        hit = [w for w in warnings if exp.warning_contains.lower() in w.lower()]
        checks.append((f"warning:{exp.warning_contains}", bool(hit),
                       f"warnings were {warnings}"))
    return checks


def score_baseline(exp: Expected, resp) -> list[Check]:
    """Old text flow: only vendor, tax ID, grand total and line totals exist."""
    if not resp.success or resp.data is None:
        return [("extraction", False, f"failed: {resp.error}")]
    d = resp.data
    totals = [ln.total_price for ln in d.order_lines]
    missing = [e for e in exp.active if not any(money_ok(t, e) for t in totals)]
    return [
        ("vendor", vendor_ok(d.vendor_name, exp.vendor),
         f"actual {d.vendor_name!r} vs expected one of {exp.vendor}"),
        ("tax_id", tax_id_ok(d.vat_id, exp.tax_id), f"actual {d.vat_id!r} vs expected {exp.tax_id!r}"),
        ("active_lines", not missing,
         f"expected active totals missing from output: {missing}; actual {sorted(totals)}"),
        ("grand", money_ok(d.total_cost, exp.grand), f"actual {d.total_cost} vs expected {exp.grand}"),
    ]


# --------------------------------------------------------------------------
# Environment (in-memory SQLite seeded like the Acme org)
# --------------------------------------------------------------------------


def build_db():
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.base import Base
    from app.models import Organization
    from app.seed import (
        seed_acme_booking_groups,
        seed_articles,
        seed_commodity_groups,
        seed_suppliers,
    )
    from app.services.commodity_groups import copy_templates

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    org = Organization(name="Acme Corp", slug="acme")
    db.add(org)
    db.flush()
    seed_commodity_groups(db)
    db.flush()
    copy_templates(db, org.id)
    seed_acme_booking_groups(db)
    seed_suppliers(db, org.id)
    seed_articles(db, org.id)
    db.commit()
    org_id = db.scalar(select(Organization.id).where(Organization.slug == "acme"))
    return db, org_id


def set_model(model: str) -> None:
    from app.core.config import settings

    settings.openai_model = model
    # gpt-6.1-sol takes no temperature; other models run at 0.
    settings.openai_temperature = None if model == "gpt-6.1-sol" else 0.0


# --------------------------------------------------------------------------
# Classification checks (synthetic quote text through the extraction prompt)
# --------------------------------------------------------------------------


def classify(db, org_id, text: str):
    from app.services import extraction as ex
    from app.core.config import settings
    from app.schemas.extraction import LlmQuote
    from app.services.commodity_groups import active_groups

    groups = active_groups(db, org_id)
    names = {g.id: g.name for g in groups}
    rows = "\n".join([
        "[r1] Vendor Quote  Example Supply Co.",
        "[r2] Quote to: Acme Corp",
        "[r3] Description  Qty  Unit price  Total",
        f"[r4] {text}  1  $100.00  $100.00",
        "[r5] Total $100.00",
    ])
    kwargs: dict = dict(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": ex.PDF_SYSTEM_PROMPT},
            {"role": "user", "content": ex._build_pdf_prompt(rows, ex.format_groups(groups))},
        ],
        response_format=LlmQuote,
        max_completion_tokens=8000,
    )
    if settings.openai_temperature is not None:
        kwargs["temperature"] = settings.openai_temperature
    parsed = ex._get_client().chat.completions.parse(**kwargs).choices[0].message.parsed
    priced = [ln for ln in parsed.lines if ln.kind != "heading"]
    gid = priced[0].commodity_group_id if priced else None
    return names.get(gid), gid


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


def print_table(title: str, header: list[str], rows: list[list[str]]) -> None:
    print(f"\n### {title}\n")
    print("| " + " | ".join(header) + " |")
    print("|" + "|".join("---" for _ in header) + "|")
    for r in rows:
        print("| " + " | ".join(r) + " |")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quotes-dir", default=None)
    ap.add_argument("--models", default="gpt-4o-mini,gpt-6.1-sol")
    ap.add_argument("--baseline", action="store_true", help="also score the old text flow")
    ap.add_argument("--skip-catalog", action="store_true")
    ap.add_argument("--skip-classification", action="store_true")
    args = ap.parse_args(argv)

    from app.core.config import settings
    from app.services.catalog_match import LineQuery, suggest
    from app.services.extraction import extract_from_pdf, extract_vendor_data
    from app.services.pdf import extract_text

    if not settings.openai_api_key:
        print("OPENAI_API_KEY is not set; load it with `set -a; . ../.env; set +a`.")
        return 0
    qdir = Path(args.quotes_dir or os.environ.get("QUOTES_DIR") or "../../quotes")
    qdir = (qdir if qdir.is_absolute() else BACKEND_DIR / qdir).resolve()
    if not qdir.is_dir():
        print(f"Quotes dir {qdir} not found; nothing to evaluate.")
        return 0

    db, org_id = build_db()
    files = {e.short: find_quote(qdir, e.file) for e in EXPECTED}
    for e in EXPECTED:
        if files[e.short] is None:
            print(f"MISSING quote for {e.short} ({e.file})")

    summary: list[list[str]] = []  # [run, passed, total, mean latency]

    def run_scores(label, runner, scorer, models_note=""):
        passed = total = 0
        lat: list[float] = []
        per_file: dict[str, list[Check]] = {}
        resps = {}
        for e in EXPECTED:
            path = files[e.short]
            if path is None:
                continue
            data = path.read_bytes()
            t0 = time.perf_counter()
            resp = runner(data)
            lat.append(time.perf_counter() - t0)
            resps[e.short] = resp
            checks = scorer(e, resp)
            per_file[e.short] = checks
            passed += sum(c[1] for c in checks)
            total += len(checks)
        rows = []
        for short, checks in per_file.items():
            rows.append([short, f"{sum(c[1] for c in checks)}/{len(checks)}",
                         ", ".join(f"{'ok' if c[1] else 'FAIL'}:{c[0]}" for c in checks)])
        print_table(f"{label} per-file{models_note}", ["file", "score", "fields"], rows)
        fails = [(s, c) for s, cs in per_file.items() for c in cs if not c[1]]
        if fails:
            print(f"\n#### {label} failures\n")
            for s, (name, _ok, detail) in fails:
                print(f"- {s} / {name}: {detail}")
        mean = statistics.mean(lat) if lat else 0.0
        summary.append([label, f"{passed}/{total}", f"{mean:.1f}s"])
        return resps

    if args.baseline:
        print("\nBaseline note: scores the old text flow (extract_vendor_data on pypdf text) "
              "from the current code (org-scoped groups), not a checkout of main; model "
              "gpt-4o-mini, the model main used. Only vendor, tax ID, line totals and grand "
              "total exist in that flow; boxes, net and tax are N/A.")
        set_model("gpt-4o-mini")
        run_scores("baseline (text flow, gpt-4o-mini)",
                   lambda b: extract_vendor_data(extract_text(b), db, org_id),
                   score_baseline)

    model_resps: dict[str, dict] = {}
    class_rows: list[list[str]] = []
    catalog_rows: list[list[str]] = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        set_model(model)
        print(f"\n=== model {model} (temperature={settings.openai_temperature}) ===")
        model_resps[model] = run_scores(
            model, lambda b: extract_from_pdf(b, db, org_id), score_extraction)

        if not args.skip_classification:
            for text, want, forbidden in CLASSIFICATION_CASES:
                try:
                    got, _gid = classify(db, org_id, text)
                except Exception as exc:  # noqa: BLE001
                    got = f"ERROR {type(exc).__name__}: {exc}"
                if want is not None:
                    ok = got == want
                    expect = f"= {want}"
                else:
                    ok = got is not None and got not in forbidden
                    expect = f"not in {list(forbidden)}"
                class_rows.append([model, text, str(got), expect, "PASS" if ok else "FAIL"])

        if not args.skip_catalog:
            resps = model_resps[model]
            for short, label, want in (("verdeform", "verdeform line 1", "HAR-00172"),
                                       ("hartwell", "Hartwell line 1", None)):
                resp = resps.get(short)
                if resp is None or not resp.success or not resp.data.order_lines:
                    catalog_rows.append([model, label, "no extraction", str(want), "FAIL"])
                    continue
                ln = resp.data.order_lines[0]
                q = LineQuery(ln.position_description, ln.unit or None, ln.unit_price or None,
                              None)
                got = [s.article.article_number for s in suggest(db, org_id, [q])[0]]
                ok = (want in got) if want else not got
                catalog_rows.append([model, f"{label} ({ln.position_description[:50]!r})",
                                     str(got), str(want or "none"), "PASS" if ok else "FAIL"])

    print_table("Summary", ["run", "score", "mean latency / PDF"], summary)
    if class_rows:
        print_table("Classification", ["model", "text", "chosen group", "expected", "result"],
                    class_rows)
    if catalog_rows:
        print_table("Catalog suggestions", ["model", "line", "suggested", "expected", "result"],
                    catalog_rows)
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)
