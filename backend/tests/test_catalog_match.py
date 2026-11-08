"""Catalog suggestions: embeddings shortlist + LLM confirmation, org-scoped.

Fake embeddings (fixed vectors keyed by normalised text) and a fake chat parse;
no network. TestClient over in-memory SQLite.
"""
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models import Article, ArticleEmbedding, Supplier
from app.services import catalog_match as cm
from app.services import extraction

engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)


@event.listens_for(engine, "connect")
def _fk_on(dbapi_connection, _record):
    cur = dbapi_connection.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base.metadata.create_all(engine)

MOSS = "Custom moss wall, preserved reindeer moss, made to measure"
LAPTOP = 'Business laptop 13" ultralight / 16 GB / 1 TB'
MOSS_LINE = "verdeform CUSTOM - sq ft Module - forest & ball moss"
MAC_LINE = '13" MacBook Air M2 16GB'
GRINDER_LINE = "Hartwell grinding machine HG-200, 3 kW"
KEYBOARD = "Wireless keyboard and mouse set compact"

# Fixed vectors keyed by normalised text (lowercase, collapsed whitespace).
VECTORS = {
    cm.normalize(MOSS): [1, 0, 0, 0],
    cm.normalize(MOSS_LINE): [0.95, 0.05, 0, 0],
    cm.normalize(LAPTOP): [0, 1, 0, 0],
    cm.normalize(MAC_LINE): [0.05, 0.95, 0, 0],
    cm.normalize(GRINDER_LINE): [0, 0, 0, 1],
    cm.normalize("Monitor 24-inch"): [0, 0, 1, 0],
}
DEFAULT_VECTOR = [0, 0.3, 0.3, 0]


class FakeClient:
    """Fake OpenAI client: records embedding calls and answers chat.parse."""

    def __init__(self, confirm=None, boom=False):
        self.embedding_calls: list[list[str]] = []
        self.chat_calls: list[dict] = []
        self.confirm = confirm or {}  # article_number -> match_type
        self.extra_ids: list[str] = []  # ids outside every shortlist
        self.boom = boom
        self.embeddings = SimpleNamespace(create=self._embed)
        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=self._parse))

    def _embed(self, *, model, input):
        if self.boom:
            raise RuntimeError("embeddings down")
        self.embedding_calls.append(list(input))
        data = [
            SimpleNamespace(embedding=VECTORS.get(cm.normalize(t), DEFAULT_VECTOR))
            for t in input
        ]
        return SimpleNamespace(data=data)

    def _parse(self, **kwargs):
        self.chat_calls.append(kwargs)
        payload = json.loads(kwargs["messages"][-1]["content"])
        rf = kwargs["response_format"]
        lines = []
        for ln in payload["lines"]:
            matches = [
                {"article_id": c["article_id"], "match_type": self.confirm[c["article_number"]]}
                for c in ln["candidates"]
                if c["article_number"] in self.confirm
            ]
            matches += [{"article_id": i, "match_type": "same"} for i in self.extra_ids]
            lines.append({"line_index": ln["index"], "matches": matches})
        parsed = rf.model_validate({"lines": lines})
        msg = SimpleNamespace(parsed=parsed, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])


def _get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def client():
    previous = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = _get_db
    with Session() as s:
        s.execute(delete(ArticleEmbedding))
        s.commit()
    yield TestClient(app)
    if previous is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous


def make_org(client):
    r = client.post(
        "/auth/signup",
        json={"email": f"u{uuid.uuid4().hex[:8]}@example.com", "password": "secret123"},
    )
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    slug = f"org-{uuid.uuid4().hex[:8]}"
    r = client.post("/organizations", json={"name": slug, "slug": slug}, headers=headers)
    assert r.status_code == 201, r.text
    return headers, uuid.UUID(r.json()["id"])


def seed_catalog(org_id, rows):
    """rows: (number, supplier, description, price, unit)."""
    with Session() as s:
        suppliers = {}
        for number, sup, desc, price, unit in rows:
            if sup not in suppliers:
                sp = Supplier(organization_id=org_id, name=sup)
                s.add(sp)
                s.flush()
                suppliers[sup] = sp
            s.add(
                Article(
                    organization_id=org_id,
                    article_number=number,
                    supplier_id=suppliers[sup].id,
                    description=desc,
                    unit_price=price,
                    unit=unit,
                    quantity=1,
                )
            )
        s.commit()


CATALOG = [
    ("HAR-00172", "Harborview Facility LLC", MOSS, 44.59, "sq ft"),
    ("LAP-1", "Hudson Systems Inc.", LAPTOP, 1499.0, "piece"),
    ("LAP-2", "Hudson Systems Inc.", LAPTOP + " v2", 1399.0, "piece"),
    ("LAP-3", "Hudson Systems Inc.", LAPTOP + " v3", 1299.0, "piece"),
    ("LAP-4", "Hudson Systems Inc.", LAPTOP + " v4", 1599.0, "piece"),
    ("HUD-00016", "Hudson Systems Inc.", KEYBOARD, 67.0, "set"),
    ("MON-1", "Hudson Systems Inc.", "Monitor 24-inch", 325.0, "piece"),
]


@pytest.fixture
def org(client):
    headers, org_id = make_org(client)
    seed_catalog(org_id, CATALOG)
    return headers, org_id


def use(monkeypatch, fake, key="sk-test"):
    monkeypatch.setattr(extraction, "_get_client", lambda: fake)
    monkeypatch.setattr(settings, "openai_api_key", key)
    return fake


def suggest(client, headers, lines):
    r = client.post("/articles/suggestions", json={"lines": lines}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_moss_wall_matches_har_00172(client, org, monkeypatch):
    headers, _ = org
    use(monkeypatch, FakeClient({"HAR-00172": "same"}))
    out = suggest(client, headers, [{"description": MOSS_LINE, "unit": "sq ft", "unitPrice": 52.0}])
    assert len(out) == 1 and out[0]["lineIndex"] == 0
    (s,) = out[0]["suggestions"]
    assert s["articleNumber"] == "HAR-00172"
    assert s["supplierName"] == "Harborview Facility LLC"
    assert s["description"] == MOSS
    assert s["unitPrice"] == 44.59
    assert s["unit"] == "sq ft"
    assert s["quantity"] == 1
    assert s["matchType"] == "same"
    assert s["savingPerUnit"] == 7.41
    assert set(s) == {
        "articleId", "articleNumber", "supplierName", "description", "unitPrice",
        "unit", "quantity", "matchType", "savingPerUnit",
    }


def test_grinding_machine_no_match(client, org, monkeypatch):
    headers, _ = org
    use(monkeypatch, FakeClient({}))
    out = suggest(client, headers, [{"description": GRINDER_LINE, "unit": "piece", "unitPrice": 900.0}])
    assert out == [{"lineIndex": 0, "suggestions": []}]


def test_other_org_articles_excluded(client, org, monkeypatch):
    headers, _ = org
    _, other_org = make_org(client)
    seed_catalog(other_org, [("OTH-1", "Other Supplier", MOSS, 10.0, "sq ft")])
    fake = use(monkeypatch, FakeClient({"HAR-00172": "same", "OTH-1": "same"}))
    out = suggest(client, headers, [{"description": MOSS_LINE, "unit": "sq ft", "unitPrice": 52.0}])
    assert [s["articleNumber"] for s in out[0]["suggestions"]] == ["HAR-00172"]
    sent = json.loads(fake.chat_calls[0]["messages"][-1]["content"])
    numbers = {c["article_number"] for ln in sent["lines"] for c in ln["candidates"]}
    assert "OTH-1" not in numbers


def test_requires_auth(client):
    r = client.post("/articles/suggestions", json={"lines": []})
    assert r.status_code == 401


def test_fallback_without_api_key(client, org, monkeypatch):
    headers, _ = org
    fake = use(monkeypatch, FakeClient({"HUD-00016": "same"}), key=None)
    out = suggest(
        client, headers,
        [
            {"description": "Wireless keyboard and mouse set, compact", "unit": "set", "unitPrice": 80.0},
            {"description": MOSS_LINE, "unit": "sq ft", "unitPrice": 52.0},
        ],
    )
    (s,) = out[0]["suggestions"]
    assert s["articleNumber"] == "HUD-00016"
    assert s["matchType"] == "similar"
    assert s["savingPerUnit"] == 13.0
    # Fuzzy only: the moss line scores ~54 against HAR-00172, below the cutoff.
    assert out[1]["suggestions"] == []
    out = suggest(client, headers, [{"description": GRINDER_LINE, "unit": "piece", "unitPrice": 900.0}])
    assert out[0]["suggestions"] == []
    assert fake.embedding_calls == [] and fake.chat_calls == []


def test_fuzzy_scores_are_what_the_brief_says():
    from rapidfuzz import fuzz
    from rapidfuzz.utils import default_process

    moss = fuzz.token_set_ratio(MOSS_LINE, MOSS, processor=default_process)
    kb = fuzz.token_set_ratio(
        "Wireless keyboard and mouse set, compact", KEYBOARD, processor=default_process
    )
    assert moss < 60 and kb >= 80


def test_embedding_shortlist_finds_synonym(client, org, monkeypatch):
    _, org_id = org
    fake = use(monkeypatch, FakeClient())
    with Session() as db:
        articles = list(db.scalars(select(Article).where(Article.organization_id == org_id)))
        vectors = cm.ensure_embeddings(db, [a.description for a in articles] + [MAC_LINE])
    hits = cm.shortlist(MAC_LINE, articles, k=3, vectors=vectors)
    assert hits[0].article.description == LAPTOP
    assert hits[0].mode == "embedding"
    assert hits[0].score > 0.9
    assert len(hits) == 3
    assert len(fake.embedding_calls) == 1


def test_embeddings_cached_by_text_hash(client, monkeypatch):
    fake = use(monkeypatch, FakeClient())
    texts = [MOSS, LAPTOP, "  " + MOSS.upper() + "  "]  # third normalises to the first
    with Session() as db:
        first = cm.ensure_embeddings(db, texts)
        assert len(fake.embedding_calls) == 1
        assert len(fake.embedding_calls[0]) == 2  # distinct normalised texts only
        second = cm.ensure_embeddings(db, texts)
        assert len(fake.embedding_calls) == 1  # 0 new requests
        assert second == first
        assert db.scalar(select(func.count()).select_from(ArticleEmbedding)) == 2


def test_embedding_model_mismatch_counts_as_missing(client, monkeypatch):
    fake = use(monkeypatch, FakeClient())
    with Session() as db:
        cm.ensure_embeddings(db, [MOSS])
        row = db.get(ArticleEmbedding, cm.text_hash(MOSS))
        row.model = "old-model"
        db.commit()
        cm.ensure_embeddings(db, [MOSS])
        db.expire_all()  # the upsert bypasses the ORM identity map; drop the stale row
        assert len(fake.embedding_calls) == 2
        assert db.get(ArticleEmbedding, cm.text_hash(MOSS)).model == settings.openai_embedding_model


def test_text_hash_normalisation():
    assert cm.text_hash("  Foo   BAR ") == cm.text_hash("foo bar")
    assert len(cm.text_hash("x")) == 64


def test_sorted_cheapest_first_and_max_three(client, org, monkeypatch):
    headers, _ = org
    use(monkeypatch, FakeClient({"LAP-1": "same", "LAP-2": "similar", "LAP-3": "similar", "LAP-4": "same"}))
    out = suggest(client, headers, [{"description": MAC_LINE, "unit": "piece", "unitPrice": 1350.0}])
    sug = out[0]["suggestions"]
    assert [s["articleNumber"] for s in sug] == ["LAP-3", "LAP-2", "LAP-1"]
    assert [s["unitPrice"] for s in sug] == [1299.0, 1399.0, 1499.0]
    # Negative savings are still returned.
    assert [s["savingPerUnit"] for s in sug] == [51.0, -49.0, -149.0]


def test_ids_outside_shortlist_are_ignored(client, org, monkeypatch):
    headers, _ = org
    fake = use(monkeypatch, FakeClient({"HAR-00172": "same"}))
    fake.extra_ids = [str(uuid.uuid4())]
    with Session() as s:
        mon = s.scalar(select(Article).where(Article.article_number == "MON-1"))
        fake.extra_ids.append(str(mon.id))  # a real org article, but not shortlisted
    out = suggest(client, headers, [{"description": MOSS_LINE, "unit": "sq ft", "unitPrice": 52.0}])
    assert [s["articleNumber"] for s in out[0]["suggestions"]] == ["HAR-00172"]


def test_saving_null_without_unit_price(client, org, monkeypatch):
    headers, _ = org
    use(monkeypatch, FakeClient({"HAR-00172": "same"}))
    out = suggest(client, headers, [{"description": MOSS_LINE, "unit": "sq ft"}])
    (s,) = out[0]["suggestions"]
    assert s["savingPerUnit"] is None
    out = suggest(client, headers, [{"description": MOSS_LINE, "unit": "sq ft", "unitPrice": None}])
    assert out[0]["suggestions"][0]["savingPerUnit"] is None


def test_vendor_item_number_exact_hit(client, org, monkeypatch):
    headers, _ = org
    # Embeddings are down and the text is unrelated: only the item number matches.
    fake = use(monkeypatch, FakeClient(boom=True), key=None)
    out = suggest(
        client, headers,
        [{"description": "zzz unrelated text", "unit": "set", "unitPrice": 80.0, "vendorItemNumber": "hud-00016"}],
    )
    (s,) = out[0]["suggestions"]
    assert s["articleNumber"] == "HUD-00016" and s["matchType"] == "same"
    assert fake.chat_calls == []


def test_vendor_item_number_always_in_shortlist_with_embeddings(client, org, monkeypatch):
    _, org_id = org
    use(monkeypatch, FakeClient())
    with Session() as db:
        articles = list(db.scalars(select(Article).where(Article.organization_id == org_id)))
        vectors = cm.ensure_embeddings(db, [a.description for a in articles] + [MOSS_LINE])
    hits = cm.shortlist(MOSS_LINE, articles, k=1, vectors=vectors, vendor_item_number="MON-1")
    assert {h.article.article_number for h in hits} == {"HAR-00172", "MON-1"}
    assert [h.exact for h in hits if h.article.article_number == "MON-1"] == [True]


def test_embeddings_error_falls_back_to_fuzzy(client, org, monkeypatch, caplog):
    headers, _ = org
    fake = use(monkeypatch, FakeClient({"HUD-00016": "same"}, boom=True))
    with caplog.at_level(logging.ERROR):
        out = suggest(
            client, headers,
            [{"description": "Wireless keyboard and mouse set, compact", "unit": "set", "unitPrice": 80.0}],
        )
    # Shortlisted by fuzzy matching, then confirmed by the (working) LLM.
    assert [s["articleNumber"] for s in out[0]["suggestions"]] == ["HUD-00016"]
    assert out[0]["suggestions"][0]["matchType"] == "same"
    assert len(fake.chat_calls) == 1
    assert any("embedding" in r.getMessage().lower() for r in caplog.records)


def test_llm_error_falls_back_to_fuzzy_80(client, org, monkeypatch, caplog):
    headers, _ = org
    fake = use(monkeypatch, FakeClient())

    def boom(**_kw):
        raise RuntimeError("llm down")

    fake.chat.completions.parse = boom
    with caplog.at_level(logging.ERROR):
        out = suggest(
            client, headers,
            [
                {"description": "Wireless keyboard and mouse set, compact", "unit": "set", "unitPrice": 80.0},
                {"description": MOSS_LINE, "unit": "sq ft", "unitPrice": 52.0},
            ],
        )
    assert [s["articleNumber"] for s in out[0]["suggestions"]] == ["HUD-00016"]
    assert out[1]["suggestions"] == []  # moss shortlisted by embeddings but fuzzy < 80
    assert any("llm" in r.getMessage().lower() for r in caplog.records)


def test_llm_called_once_for_all_lines_with_strict_schema(client, org, monkeypatch):
    headers, _ = org
    fake = use(monkeypatch, FakeClient({"HAR-00172": "same"}))
    suggest(
        client, headers,
        [
            {"description": MOSS_LINE, "unit": "sq ft", "unitPrice": 52.0},
            {"description": MAC_LINE, "unit": "piece", "unitPrice": 1.0},
        ],
    )
    assert len(fake.chat_calls) == 1
    assert "temperature" not in fake.chat_calls[0]  # settings.openai_temperature is None
    schema = fake.chat_calls[0]["response_format"].model_json_schema()

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert set(node["properties"]) == set(node.get("required", []))
                assert "additionalProperties" not in node or node["additionalProperties"] is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)


def test_empty_lines_and_blank_description(client, org, monkeypatch):
    headers, _ = org
    fake = use(monkeypatch, FakeClient())
    assert suggest(client, headers, []) == []
    out = suggest(client, headers, [{"description": "  ", "unit": "x", "unitPrice": 1}])
    assert out == [{"lineIndex": 0, "suggestions": []}]
    assert fake.chat_calls == []


def test_request_caps_return_422(client, org, monkeypatch):
    headers, _ = org
    use(monkeypatch, FakeClient())

    def post(lines):
        return client.post("/articles/suggestions", json={"lines": lines}, headers=headers)

    assert post([{"description": "x" * 500}]).status_code == 200
    assert post([{"description": "x" * 501}]).status_code == 422
    assert post([{"description": "a"}] * 100).status_code == 200
    assert post([{"description": "a"}] * 101).status_code == 422
    assert post([{"description": "a", "unit": "u" * 100, "vendorItemNumber": "v" * 100}]).status_code == 200
    assert post([{"description": "a", "unit": "u" * 101}]).status_code == 422
    assert post([{"description": "a", "vendorItemNumber": "v" * 101}]).status_code == 422


def test_upsert_statement_on_postgresql():
    from sqlalchemy.dialects import postgresql

    stmt = postgresql.insert(ArticleEmbedding).values(
        [{"text_hash": "h", "model": "m", "embedding": [1.0]}]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["text_hash"],
        set_={"model": stmt.excluded.model, "embedding": stmt.excluded.embedding},
    )
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT (text_hash) DO UPDATE" in sql
    assert "model = excluded.model" in sql and "embedding = excluded.embedding" in sql


def test_upsert_rejects_unknown_dialect():
    fake_db = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="mysql")))
    with pytest.raises(NotImplementedError):
        cm._upsert_embeddings(fake_db, [{"text_hash": "h", "model": "m", "embedding": [1.0]}])


@pytest.mark.parametrize("old_model", [settings.openai_embedding_model, "old-model"])
def test_upsert_tolerates_existing_row(client, monkeypatch, old_model):
    """A row inserted by a concurrent request (or a stale model) is overwritten."""
    use(monkeypatch, FakeClient())
    h = cm.text_hash(MOSS)
    with Session() as db:
        db.add(ArticleEmbedding(text_hash=h, model=old_model, embedding=[9.0]))
        db.commit()
        cm._upsert_embeddings(
            db,
            [{"text_hash": h, "model": settings.openai_embedding_model, "embedding": [1.0, 2.0]}],
        )
        db.commit()
        row = db.get(ArticleEmbedding, h)
        db.refresh(row)
        assert row.model == settings.openai_embedding_model
        assert row.embedding == [1.0, 2.0]
        assert db.scalar(select(func.count()).select_from(ArticleEmbedding)) == 1
