# Lio Procurement App

Upload vendor-offer PDFs, extract the details with AI, and track procurement
requests across your organization.

- **`frontend/`** — Next.js 15, React 19, Tailwind CSS 4
- **`backend/`** — FastAPI, SQLAlchemy, Alembic, JWT auth
- **Database** — PostgreSQL 16

## Prerequisites

- **Docker Desktop** (or Docker Engine) with **Docker Compose v2** — running before you start.
- An **OpenAI API key** is optional: the app boots and runs without it; only PDF extraction needs one.

## Quick start (Docker)

```bash
cp .env.example .env      # optional: add your OpenAI API key for PDF extraction
docker compose up --build
```

| | URL |
| --- | --- |
| App | http://localhost:3200 |
| API + docs | http://localhost:7200 · http://localhost:7200/docs |

Sign in with a demo account — no registration needed (password `demo1234`).
Development happens in the **Acme Corp** organization, which has one of each role:

| Account | Role | Can do |
| --- | --- | --- |
| `admin@acme.com` | admin | everything incl. members & settings |
| `buyer@acme.com` | buyer | approve requests |
| `requester@acme.com` | requester | create & track requests |

Acme requests below $2,000, including tax, are approved automatically. Requests
at or above that amount need a buyer or admin (see the **Approvals** tab). Admins
can change or disable the threshold in Organization settings. A price edit that
changes an approved request's total re-evaluates its approval.

Status follows approval: an approved request moves from Open to In progress, and a
rejected one is closed. If an edit sends an approved request back to pending, it
reopens. Anyone can still change the status by hand.

The PDF form extracts items, discounts, charges and alternatives, with eye
buttons linking fields, warnings and lines to their source boxes in the PDF.
Requestor Name is prefilled with the person the quote is addressed to (e.g.
"Prepared for"), unless you already typed one. Department is filled only when
the quote names one. A value the AI worked out rather than copied links to what
it came from: the title to the rows naming the purchase, a validity date
computed from "valid for four weeks" to that sentence and the quote date. The
total links to the quote's printed grand total, and the primary commodity group
to the lines booked to it.
Drafts save automatically and can be resumed from Requests. Until a request is
closed, you can edit its lines, swap alternatives and apply negotiated catalog
prices. Every editable item has a **Replace from catalog** picker with search and supplier filtering.
Review one line at a time; changing units requires a new quantity. The saved
request links to the catalog article, and **Restore quoted item** restores the
original values even after reloading. Catalog replacements use the request
currency; the app does not convert currencies or infer quantities across units.

Commodity groups are assigned per line using your organization's booking
descriptions. Admins manage these groups in Organization settings. The request's
primary group is computed from its largest active spend.

Extraction needs a PDF text layer. Scans and image-only header fields (including
verdeform's EIN) need OCR, which is not implemented; missing values stay empty.

Other tenants exist to show the multi-tenant setup — **Walmart, Schaeffler,
Siemens, BMW** — each with an admin at `admin@<company>.com` (same password).
Their data is fully isolated from Acme's.

On startup the backend applies schema migrations and seeds the database (demo
accounts, organizations, commodity groups, suppliers) — both steps are
idempotent, so restarts never duplicate or overwrite anything (your data lives
in the `postgres_data` volume; `docker compose down -v` resets it). The OpenAI
key is only needed for PDF extraction — everything else works without it.

## Local development

If you previously ran this PR through migration `0011`, its schema is already
current. After rebuilding the backend image, update only its revision marker:
`docker compose run --rm --no-deps backend alembic stamp --purge 0007`.
Fresh databases and databases at `0006` use the normal startup migration.

Requests extracted before every field had a source highlight can get the
missing ones from their stored PDFs. This re-runs extraction (needs the OpenAI
key) but only adds highlights; stored values and highlights stay:
`docker compose exec backend python -m app.backfill_field_sources`.

```bash
docker compose up db        # Postgres only

# backend  → http://localhost:7200
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
alembic upgrade head        # schema
python -m app.seed          # reference data (idempotent)
uvicorn app.main:app --reload --port 7200

# frontend → http://localhost:3200
cd frontend
npm install
npm run dev
```

## Tests

```bash
cd backend && python -m pytest      # API tests (in-memory SQLite)
cd frontend && npx tsc --noEmit && npm run build
```

## Layout

```
backend/    FastAPI app — routers, services, models, schemas, core; Alembic migrations
frontend/   Next.js app — app-router pages, typed API client (src/lib/api), components
docker-compose.yml
```
