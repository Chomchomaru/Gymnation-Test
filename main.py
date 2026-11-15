import calendar
import re
import threading
from datetime import date
from decimal import Decimal
from typing import Literal, NoReturn
from uuid import uuid4

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from billing import invoices_between
from phone_validation import PhoneError, lookup_phone, member_phone
from reconciliation import reconcile, row_errors

app = FastAPI()

Country = Literal["AE", "SA", "BH"]
# Currency and VAT come from the country, never from the caller
COUNTRIES = {
    "AE": {"currency": "AED", "vat_rate": Decimal("0.05")},
    "SA": {"currency": "SAR", "vat_rate": Decimal("0.15")},
    "BH": {"currency": "BHD", "vat_rate": Decimal("0.10")},
}

# ponytail: in-memory store + one global lock; fine for one process, each run starts empty (spec).
members: dict[str, dict] = {}
phone_index: dict[str, str] = {}  # E.164 phone -> member id
plans: dict[str, dict] = {}  # caller-chosen id -> plan (with internal vat_rate)
agreements: dict[str, dict] = {}  # generated id -> agreement; a member may hold several
runs: dict[str, dict] = {}  # reconciliation run id -> stored response body (frozen at creation)
write_lock = threading.Lock()


# ---------- errors: always {"error": {"code": ..., "message": ..., ...extra}} ----------

class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str | None = None, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra


def fail(status: int, code: str, message: str | None = None, **extra) -> NoReturn:
    raise ApiError(status, code, message, **extra)


def error_response(status: int, code: str, message: str | None = None, **extra) -> JSONResponse:
    body = {"code": code, "message": message or code.replace("_", " ").capitalize(), **extra}
    return JSONResponse({"error": jsonable_encoder(body)}, status_code=status)


@app.exception_handler(ApiError)
async def api_error(_: Request, exc: ApiError):
    return error_response(exc.status, exc.code, exc.message, **exc.extra)


@app.exception_handler(PhoneError) 
async def phone_error(_: Request, exc: PhoneError):
    return error_response(exc.status, exc.code, None, **exc.extra)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    errors = jsonable_encoder(exc.errors())
    first = errors[0] if errors else {}
    where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    return error_response(422, "VALIDATION", f"{where}: {first.get('msg', 'invalid input')}", details=errors)


@app.exception_handler(StarletteHTTPException)
async def http_error(_: Request, exc: StarletteHTTPException):
    code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
    return error_response(exc.status_code, code, str(exc.detail))


# ---------- dates ----------

ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_iso_date(v, field: str) -> date:
    """Strict YYYY-MM-DD; rejects timestamps, datetimes and other spellings."""
    if isinstance(v, str) and ISO_DATE.fullmatch(v):
        try:
            return date.fromisoformat(v)
        except ValueError:
            pass
    raise ValueError(f"{field} must be a valid YYYY-MM-DD date")


# ---------- health ----------

@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Part 1: members ----------

class MemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)  # checked after trimming
    country: Country
    phone: str

    @field_validator("name", mode="before")
    @classmethod
    def trim(cls, v):
        return v.strip() if isinstance(v, str) else v


@app.post("/members", status_code=201)
def create_member(body: MemberIn):
    phone = member_phone(body.phone, body.country)
    with write_lock:  # check + insert atomically so concurrent writes can't share a phone
        if phone in phone_index:
            fail(409, "PHONE_TAKEN", existing_member_id=phone_index[phone])
        member = {"id": str(uuid4()), "name": body.name, "country": body.country, "phone": phone}
        members[member["id"]] = member
        phone_index[phone] = member["id"]
    return member


@app.get("/members/lookup")
def lookup_member(phone: str, country: Country | None = None):
    member_id = phone_index.get(lookup_phone(phone, country))
    if not member_id:
        fail(404, "NOT_FOUND", "no member has that phone")
    return members[member_id]


# ---------- Part 2: plans, agreements, invoices ----------

class PlanIn(BaseModel):
    id: str = Field(min_length=1)
    country: Country
    # integer minor units (halalas / fils), before VAT; strict rejects 199.99, "100", true
    monthly: int = Field(gt=0, strict=True)
    joining_fee: int = Field(ge=0, strict=True)


@app.post("/plans", status_code=201)
def create_plan(body: PlanIn):
    public = {"id": body.id, "country": body.country, "currency": COUNTRIES[body.country]["currency"],
              "monthly": body.monthly, "joining_fee": body.joining_fee}
    with write_lock:
        if body.id in plans:
            fail(409, "PLAN_EXISTS", id=body.id)
        plans[body.id] = {**public, "vat_rate": COUNTRIES[body.country]["vat_rate"]}
    return public


class AgreementIn(BaseModel):
    member_id: str
    plan_id: str
    start_date: date  # "YYYY-MM-DD" only, see validator
    due_day: int = Field(ge=1, le=31, strict=True)

    @field_validator("start_date", mode="before")
    @classmethod
    def iso_date_only(cls, v):
        return parse_iso_date(v, "start_date")


@app.post("/agreements", status_code=201)
def create_agreement(body: AgreementIn):
    member = members.get(body.member_id) or fail(422, "UNKNOWN_MEMBER", member_id=body.member_id)
    plan = plans.get(body.plan_id) or fail(422, "UNKNOWN_PLAN", plan_id=body.plan_id)
    if plan["country"] != member["country"]:
        fail(422, "PLAN_COUNTRY_MISMATCH", member_country=member["country"], plan_country=plan["country"])
    agreement = {"id": str(uuid4()), **body.model_dump()}
    agreements[agreement["id"]] = agreement
    return agreement


@app.get("/agreements/{agreement_id}/invoices")
def list_invoices(agreement_id: str, frm: str = Query(alias="from"), to: str = Query()):
    try:
        start, end = parse_iso_date(frm, "from"), parse_iso_date(to, "to")
    except ValueError as e:
        fail(422, "VALIDATION", str(e))
    agreement = agreements.get(agreement_id) or fail(404, "NOT_FOUND", "unknown agreement")
    if end < start or (end - start).days > 366:  # "more than 366 days after from"
        fail(422, "INVALID_RANGE", "to must be on or after from, and at most 366 days after it")
    return invoices_between(agreement, plans[agreement["plan_id"]], start, end)


# ---------- Part 3: reconciliation ----------

class GatewayRow(BaseModel):
    gateway_txn_id: str = Field(min_length=1)
    agreement_id: str
    amount: int = Field(gt=0, strict=True)  # minor units, VAT included
    currency: str
    status: Literal["SETTLED", "DECLINED"]
    attempted_on: date

    @field_validator("attempted_on", mode="before")
    @classmethod
    def iso_date_only(cls, v):
        return parse_iso_date(v, "attempted_on")


class RunIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")  # YYYY-MM
    rows: list[GatewayRow]


@app.post("/reconciliation/runs", status_code=201)
def create_run(body: RunIn):
    year, month = map(int, body.month.split("-"))
    first, last = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    rows = [r.model_dump() for r in body.rows]
    snapshot_agreements, snapshot_plans = dict(agreements), dict(plans)  # consistent view while computing

    errors = row_errors(rows, first, last, snapshot_agreements, snapshot_plans)
    if errors:
        fail(422, "INVALID_ROWS", errors=errors)
    run = {"id": str(uuid4()), "month": body.month,
           **reconcile(rows, first, last, snapshot_agreements, snapshot_plans)}
    runs[run["id"]] = jsonable_encoder(run)
    return runs[run["id"]]


@app.get("/reconciliation/runs/{run_id}")
def get_run(run_id: str):
    return runs.get(run_id) or fail(404, "NOT_FOUND", "unknown reconciliation run")
