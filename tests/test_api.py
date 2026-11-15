import random
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import main

c = TestClient(main.app)


@pytest.fixture(autouse=True)
def empty_store():
    for store in (main.members, main.phone_index, main.plans, main.agreements, main.runs):
        store.clear()


def err(r):
    body = r.json()["error"]
    assert set(body) >= {"code", "message"}, body  # every error carries code + message
    return r.status_code, body["code"]


def member(country="SA", phone="055 123 4567", name="Noura Al Harbi"):
    return c.post("/members", json={"name": name, "country": country, "phone": phone})


def plan(id="anytime-sa", country="SA", monthly=29900, joining_fee=0):
    return c.post("/plans", json={"id": id, "country": country, "monthly": monthly, "joining_fee": joining_fee})


def agreement(member_id, plan_id="anytime-sa", start="2026-09-20", due_day=1):
    return c.post("/agreements", json={"member_id": member_id, "plan_id": plan_id, "start_date": start, "due_day": due_day})


def invoices(aid, frm, to):
    return c.get(f"/agreements/{aid}/invoices", params={"from": frm, "to": to})


def lookup(**params):
    return c.get("/members/lookup", params=params)


# ---------- contract ----------

def test_health():
    assert c.get("/health").status_code == 200


def test_unknown_route_is_not_found():
    assert err(c.get("/nope")) == (404, "NOT_FOUND")


# ---------- Part 1 ----------

def test_member_example_and_three_lookups():
    r = member(name="  Noura Al Harbi  ")
    assert r.status_code == 201
    m = r.json()
    assert m == {"id": m["id"], "name": "Noura Al Harbi", "country": "SA", "phone": "+966551234567"}
    for p in [{"phone": "966551234567"}, {"phone": "+966551234567"}, {"phone": "055 123 4567", "country": "SA"}]:
        assert lookup(**p).json() == m, p
    assert err(lookup(phone="+966551234568")) == (404, "NOT_FOUND")


@pytest.mark.parametrize("name", ["", "   ", "x" * 121, None, 5])
def test_member_name_rules(name):
    assert err(member(name=name)) == (422, "VALIDATION")


def test_member_name_120_ok():
    assert member(name="x" * 120).status_code == 201


@pytest.mark.parametrize("country", ["sa", "GB", "", None])
def test_member_country_rules(country):
    assert err(member(country=country)) == (422, "VALIDATION")


@pytest.mark.parametrize("phone", [
    "00966551234567",   # 00 prefix
    "0112345678",       # SA landline
    "05512345",         # too short
    "055123456789",     # too long
    "551234567",        # national without trunk 0
    "+0551234567",      # + with national
    "+96655+1234567",   # second +
    "055.123.4567",     # dots not cleaned
    "abc",
    "+447911123456",    # other country
])
def test_member_invalid_phones(phone):
    assert err(member(phone=phone)) == (422, "INVALID_PHONE")


def test_member_cleaning_and_spellings():
    assert member(phone="(055) 123-4567").json()["phone"] == "+966551234567"
    assert member(country="BH", phone="3600 1234").json()["phone"] == "+97336001234"
    assert member(country="AE", phone="971 50 123 4567").json()["phone"] == "+971501234567"


def test_member_foreign_sim_and_national_in_own_country():
    assert member(country="SA", phone="+971501234567").status_code == 201      # AE SIM on SA member
    assert err(member(country="BH", phone="0551234567")) == (422, "INVALID_PHONE")  # national read as BH
    assert err(member(country="SA", phone="36001234")) == (422, "INVALID_PHONE")


def test_phone_taken_any_spelling():
    first = member().json()["id"]
    for spelling in ["+966551234567", "966551234567", "0551234567"]:
        r = member(phone=spelling)
        assert err(r) == (409, "PHONE_TAKEN")
        assert r.json()["error"]["existing_member_id"] == first


def test_phone_taken_under_race():
    spellings = ["+971521112233", "971521112233", "052 111 2233", "(052)-111-2233"] * 10
    with ThreadPoolExecutor(20) as ex:
        statuses = list(ex.map(lambda s: member(country="AE", phone=s).status_code, spellings))
    assert statuses.count(201) == 1 and statuses.count(409) == 39


def test_lookup_rules():
    member()
    assert err(lookup(phone="0551234567")) == (422, "PHONE_NEEDS_COUNTRY")
    assert err(lookup(phone="36001234")) == (422, "PHONE_NEEDS_COUNTRY")      # fits BH shape only, still needs it
    assert err(lookup(phone="36001234", country="SA")) == (422, "INVALID_PHONE")
    assert err(lookup(phone="+966551234567", country="AE")) == (422, "PHONE_COUNTRY_MISMATCH")
    assert err(lookup(phone="966551234567", country="BH")) == (422, "PHONE_COUNTRY_MISMATCH")
    assert err(lookup(phone="00966551234567")) == (422, "INVALID_PHONE")
    assert err(lookup(phone="12345")) == (422, "INVALID_PHONE")
    assert err(lookup(phone="0551234567", country="XX")) == (422, "VALIDATION")
    assert err(c.get("/members/lookup")) == (422, "VALIDATION")


# ---------- Part 2 ----------

def test_plan_example_and_duplicate():
    r = plan()
    assert r.status_code == 201
    assert r.json() == {"id": "anytime-sa", "country": "SA", "currency": "SAR", "monthly": 29900, "joining_fee": 0}
    assert err(plan()) == (409, "PLAN_EXISTS")


@pytest.mark.parametrize("field,value", [
    ("monthly", 0), ("monthly", -1), ("monthly", 299.5), ("monthly", "29900"), ("monthly", True), ("monthly", 100.0),
    ("joining_fee", -1), ("joining_fee", 1.5), ("country", "UAE"),
])
def test_plan_validation(field, value):
    body = {"id": "p", "country": "SA", "monthly": 29900, "joining_fee": 0} | {field: value}
    assert err(c.post("/plans", json=body)) == (422, "VALIDATION")


def test_plan_exists_under_race():
    with ThreadPoolExecutor(20) as ex:
        statuses = list(ex.map(lambda _: plan().status_code, range(40)))
    assert statuses.count(201) == 1 and statuses.count(409) == 39


def test_agreement_example_and_errors():
    mid = member().json()["id"]
    plan()
    plan(id="ae", country="AE")
    r = agreement(mid)
    assert r.status_code == 201
    assert r.json() == {"id": r.json()["id"], "member_id": mid, "plan_id": "anytime-sa", "start_date": "2026-09-20", "due_day": 1}
    assert agreement(mid, due_day=31).status_code == 201  # several agreements
    assert err(agreement("ghost")) == (422, "UNKNOWN_MEMBER")
    assert err(agreement(mid, plan_id="ghost")) == (422, "UNKNOWN_PLAN")
    assert err(agreement(mid, plan_id="ae")) == (422, "PLAN_COUNTRY_MISMATCH")
    for bad in [0, 32, 1.5, "5"]:
        assert err(agreement(mid, due_day=bad)) == (422, "VALIDATION")
    for bad in ["2026-02-30", "20-09-2026", "2026-09-20T00:00:00", 1767225600]:
        assert err(agreement(mid, start=bad)) == (422, "VALIDATION")


def test_worked_example_1():
    mid = member().json()["id"]
    plan()
    aid = agreement(mid).json()["id"]
    r = invoices(aid, "2026-09-01", "2026-12-31")
    assert r.status_code == 200
    inv = r.json()
    assert inv[0] == {"invoice_no": 1, "due_date": "2026-09-20", "kind": "FIRST",
                      "proration": {"days": 11, "cycle_days": 30},
                      "net": 10963, "vat": 1644, "total": 12607, "currency": "SAR"}
    assert inv[1] == {"invoice_no": 2, "due_date": "2026-10-01", "kind": "MONTHLY", "proration": None,
                      "net": 29900, "vat": 4485, "total": 34385, "currency": "SAR"}
    assert [i["due_date"] for i in inv] == ["2026-09-20", "2026-10-01", "2026-11-01", "2026-12-01"]


def test_worked_example_2_two_invoices_in_september():
    mid = member(country="AE", phone="0501234567").json()["id"]
    plan(id="ae", country="AE", monthly=24900, joining_fee=9900)
    aid = agreement(mid, plan_id="ae", start="2026-09-05", due_day=20).json()["id"]
    inv = invoices(aid, "2026-09-01", "2026-09-30").json()
    assert [(i["invoice_no"], i["due_date"], i["kind"]) for i in inv] == [(1, "2026-09-05", "FIRST"), (2, "2026-09-20", "MONTHLY")]
    assert inv[0]["proration"] == {"days": 15, "cycle_days": 31}
    assert (inv[0]["net"], inv[0]["vat"], inv[0]["total"], inv[0]["currency"]) == (21948, 1097, 23045, "AED")


def test_due_day_31_clamps_and_numbering_does_not_restart():
    mid = member().json()["id"]
    plan()
    aid = agreement(mid, start="2027-01-31", due_day=31).json()["id"]
    inv = invoices(aid, "2027-01-01", "2027-05-31").json()
    assert [i["due_date"] for i in inv] == ["2027-01-31", "2027-02-28", "2027-03-31", "2027-04-30", "2027-05-31"]
    assert inv[0]["proration"] == {"days": 28, "cycle_days": 28} and inv[0]["net"] == 29900  # start on a due date
    assert [(i["invoice_no"], i["due_date"]) for i in invoices(aid, "2027-04-01", "2027-04-30").json()] == [(4, "2027-04-30")]
    leap = agreement(mid, start="2028-02-10", due_day=31).json()["id"]
    assert invoices(leap, "2028-02-01", "2028-02-29").json()[1]["due_date"] == "2028-02-29"


def test_bhd_vat_stays_in_fils():
    mid = member(country="BH", phone="36001234").json()["id"]
    plan(id="bh", country="BH", monthly=5790, joining_fee=0)
    aid = agreement(mid, plan_id="bh", start="2026-09-01", due_day=1).json()["id"]
    inv = invoices(aid, "2026-09-01", "2026-09-30").json()[0]
    assert (inv["vat"], inv["total"], inv["currency"]) == (579, 6369, "BHD")


def test_invoice_range_rules():
    mid = member().json()["id"]
    plan()
    aid = agreement(mid).json()["id"]
    assert err(invoices(aid, "2026-10-01", "2026-09-30")) == (422, "INVALID_RANGE")
    assert invoices(aid, "2026-01-01", "2027-01-02").status_code == 200       # exactly 366 days after
    assert err(invoices(aid, "2026-01-01", "2027-01-03")) == (422, "INVALID_RANGE")
    assert invoices(aid, "2026-09-20", "2026-09-20").json()[0]["invoice_no"] == 1  # inclusive
    assert invoices(aid, "2025-01-01", "2025-12-31").json() == []
    assert err(invoices(aid, "2026-13-01", "2026-12-31")) == (422, "VALIDATION")
    assert err(c.get(f"/agreements/{aid}/invoices", params={"from": "2026-09-01"})) == (422, "VALIDATION")
    assert err(invoices("ghost", "2026-09-01", "2026-09-30")) == (404, "NOT_FOUND")


# ---------- Part 3 ----------

def row(tid, aid, amount, currency, status="SETTLED", on="2026-10-01"):
    return {"gateway_txn_id": tid, "agreement_id": aid, "amount": amount, "currency": currency,
            "status": status, "attempted_on": on}


def run(rows, month="2026-10"):
    return c.post("/reconciliation/runs", json={"month": month, "rows": rows})


@pytest.fixture
def two_agreements():
    sa = member().json()["id"]
    bh = member(country="BH", phone="36001234").json()["id"]
    plan()
    plan(id="bh", country="BH", monthly=24500)   # total 26950
    return agreement(sa).json()["id"], agreement(bh, plan_id="bh", start="2026-09-15").json()["id"]


def test_spec_example_and_read_back(two_agreements):
    a_sa, a_bh = two_agreements
    r = run([row("T-88121", a_sa, 34385, "SAR"), row("T-88122", a_bh, 26950, "BHD", "DECLINED", "2026-10-02")])
    assert r.status_code == 201
    body = r.json()
    assert body["month"] == "2026-10"
    assert body["summary"] == {"MATCHED": 1, "DUE_DAY_DRIFT": 0, "DOUBLE_CHARGE_RISK": 0, "MISSING_FROM_LEDGER": 0, "DECLINED": 1}
    assert body["rows"] == [{"gateway_txn_id": "T-88121", "verdict": "MATCHED", "invoice": {"agreement_id": a_sa, "invoice_no": 2}},
                            {"gateway_txn_id": "T-88122", "verdict": "DECLINED", "invoice": None}]
    assert body["unmatched_invoices"] == [{"agreement_id": a_bh, "invoice_no": 2, "due_date": "2026-10-01", "total": 26950, "currency": "BHD"}]
    assert c.get(f"/reconciliation/runs/{body['id']}").json() == body
    assert err(c.get("/reconciliation/runs/ghost")) == (404, "NOT_FOUND")


def test_verdicts(two_agreements):
    a_sa, a_bh = two_agreements
    body = run([row("A", a_sa, 34386, "SAR", on="2026-10-03"),   # within 1, other day
                row("B", a_bh, 26950, "BHD"), row("C", a_bh, 26949, "BHD"),
                row("D", a_sa, 34388, "SAR"),                    # off by 3
                row("E", "ghost", 100, "XXX")]).json()
    assert {r["gateway_txn_id"]: r["verdict"] for r in body["rows"]} == {
        "A": "DUE_DAY_DRIFT", "B": "DOUBLE_CHARGE_RISK", "C": "DOUBLE_CHARGE_RISK",
        "D": "MISSING_FROM_LEDGER", "E": "MISSING_FROM_LEDGER"}
    assert body["rows"][3]["invoice"] is None and body["rows"][1]["invoice"] == {"agreement_id": a_bh, "invoice_no": 2}
    assert body["unmatched_invoices"] == []


def test_order_does_not_matter(two_agreements):
    a_sa, a_bh = two_agreements
    rows = [row("A", a_sa, 34385, "SAR", on="2026-10-02"), row("B", a_sa, 34385, "SAR"),
            row("C", a_bh, 26950, "BHD", "DECLINED"), row("D", "ghost", 5, "SAR", on="2026-10-09")]
    def canon(b):
        return b["summary"], sorted(map(str, b["rows"])), b["unmatched_invoices"]
    base = canon(run(rows).json())
    for _ in range(10):
        random.shuffle(rows)
        assert canon(run(rows).json()) == base


def test_earliest_due_date_wins():
    mid = member(country="AE", phone="0501234567").json()["id"]
    plan(id="ae", country="AE", monthly=10000)
    aid = agreement(mid, plan_id="ae", start="2026-09-20", due_day=20).json()["id"]  # FIRST 20 Sep, full price
    body = run([row("X", aid, 10500, "AED", on="2026-09-20")], month="2026-09").json()
    assert body["rows"][0] == {"gateway_txn_id": "X", "verdict": "MATCHED", "invoice": {"agreement_id": aid, "invoice_no": 1}}


def test_unmatched_sorting(two_agreements):
    body = run([]).json()
    keys = [(u["due_date"], u["agreement_id"], u["invoice_no"]) for u in body["unmatched_invoices"]]
    assert keys == sorted(keys) and len(keys) == 2


def test_invalid_rows(two_agreements):
    a_sa, a_bh = two_agreements
    r = run([row("T1", a_sa, 34385, "SAR"), row("T1", a_sa, 34385, "SAR", on="2026-11-01"),
             row("T2", a_bh, 1, "SAR", "DECLINED"), row("T3", "ghost", 1, "EUR")])
    assert err(r) == (422, "INVALID_ROWS")
    got = sorted((e["gateway_txn_id"], e["code"]) for e in r.json()["error"]["errors"])
    assert got == [("T1", "DUPLICATE_TXN"), ("T1", "DUPLICATE_TXN"), ("T1", "ROW_OUTSIDE_MONTH"), ("T2", "CURRENCY_MISMATCH")]


@pytest.mark.parametrize("body", [
    {"month": "2026-13", "rows": []},
    {"month": "2026-1", "rows": []},
    {"rows": []},
    {"month": "2026-10", "rows": [{"gateway_txn_id": "Z", "agreement_id": "a", "amount": 0, "currency": "SAR", "status": "SETTLED", "attempted_on": "2026-10-01"}]},
    {"month": "2026-10", "rows": [{"gateway_txn_id": "Z", "agreement_id": "a", "amount": 5, "currency": "SAR", "status": "PENDING", "attempted_on": "2026-10-01"}]},
    {"month": "2026-10", "rows": [{"gateway_txn_id": "Z", "agreement_id": "a", "amount": 5, "currency": "SAR", "status": "SETTLED", "attempted_on": "2026/10/01"}]},
])
def test_run_validation(body):
    assert err(c.post("/reconciliation/runs", json=body)) == (422, "VALIDATION")
