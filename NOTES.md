# Notes

## How to run

```bash
docker build -t app . && docker run -p 8080:8080 app   # API on 0.0.0.0:8080, GET /health -> 200
docker build --target test .                           # runs the pytest suite (57 tests)
```

Locally without Docker (Python 3.12+):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install fastapi==0.163.1 pydantic==2.14.3 uvicorn==0.54.0 pytest==9.2.0 httpx==0.28.1
uvicorn main:app --port 8080        # interactive docs at /docs
python -m pytest -q tests
```

Layout: `main.py` (endpoints, in-memory store, error envelope), `phone_validation.py` (Part 1 phone rules),
`billing.py` (schedule, proration, VAT), `reconciliation.py` (gateway matching), `tests/test_api.py`.

## What I skipped

- **Persistence.** State is in memory, and each run starts empty, as the contract allows. Race-safety for `PHONE_TAKEN` and `PLAN_EXISTS` uses one process-wide lock, which is correct for the single uvicorn process the container runs. It would need per-key locks or a DB unique index with multiple workers.
- **Docker wasn't built on my machine** (no Docker installed). The test stage and the run command were checked in a clean virtualenv with the same pinned versions: 57 tests passed, and `/health` returned 200 on 0.0.0.0:8080.
- **`uv.lock` is stale.** The Dockerfile pins versions itself and doesn't use uv.
- **Endpoints beyond the spec,** such as list and get for members or plans, and auth.

## Assumptions

- A phone is valid only in the shapes in the country table: national `05XXXXXXXX` (AE/SA) and `3XXXXXXX` (BH), and `+`/no-`+` dial code followed by `5XXXXXXXX` (AE/SA) or `3XXXXXXX` (BH).
- Cleaning removes only literal spaces, hyphens and round brackets. Tabs, dots and slashes are left in, so they give `INVALID_PHONE`.
- `country` must be exactly `AE`, `SA` or `BH`, case-sensitive, in bodies and in the lookup query. Anything else is `422 VALIDATION`.
- A non-string `name` is `422 VALIDATION`. Length 1–120 is checked after trimming.
- The error `message` is human-readable text. Extra fields such as `existing_member_id`, `errors` and `details` sit next to `code` and `message`.
- Malformed input anywhere gives `422 VALIDATION` with the Pydantic error list in `details`. An unknown route or id gives `404 NOT_FOUND`.
- A lookup with a valid number that nobody has gives `404 NOT_FOUND`.
- Money fields must be JSON integers. `100.0`, `"100"` and `true` are rejected.
- Plan ids are any non-empty string, compared case-sensitively.
- Start dates in the past are allowed, and a member may hold the same plan more than once.
- Agreement checks run in this order: `UNKNOWN_MEMBER`, `UNKNOWN_PLAN`, then `PLAN_COUNTRY_MISMATCH`.
- Invoice query checks run in this order: unparseable or missing `from`/`to` (`VALIDATION`), unknown agreement (`NOT_FOUND`), then `INVALID_RANGE`.
- "More than 366 days after it" means `to − from > 366` days, so `to = from + 366` is allowed.
- A start date that falls on a clamped due date (e.g. 28 Feb with due day 31) counts as a due date, which gives a full-price FIRST invoice.
- Proration is calculated in exact integer arithmetic, rounded half up. VAT is calculated in `Decimal`, rounded half up. Nothing is rounded anywhere else.
- A reconciliation row's `currency` is any string. Only `CURRENCY_MISMATCH` checks it, and only when the agreement exists.
- `CURRENCY_MISMATCH` also applies to `DECLINED` rows.
- An empty `rows` list is valid: every expected invoice is then unmatched.
- If several candidates share the earliest due date, the lower `invoice_no` wins.
- A run is a snapshot taken at creation. `GET` returns the stored body unchanged.
- Generated ids are UUID4 strings.
