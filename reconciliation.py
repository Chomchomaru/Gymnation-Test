"""Reconcile gateway rows for one month against expected invoices. Pure functions; never mutates the store."""

from collections import Counter, defaultdict
from datetime import date

from billing import invoices_between

VERDICTS = ("MATCHED", "DUE_DAY_DRIFT", "DOUBLE_CHARGE_RISK", "MISSING_FROM_LEDGER", "DECLINED")


def row_errors(rows: list[dict], first: date, last: date, agreements: dict, plans: dict) -> list[dict]:
    """One entry per offending row and code. Empty list = rows are valid."""
    counts = Counter(r["gateway_txn_id"] for r in rows)
    errors = []
    for r in rows:
        tid = r["gateway_txn_id"]
        if counts[tid] > 1:
            errors.append({"gateway_txn_id": tid, "code": "DUPLICATE_TXN"})
        if not first <= r["attempted_on"] <= last:
            errors.append({"gateway_txn_id": tid, "code": "ROW_OUTSIDE_MONTH"})
        agreement = agreements.get(r["agreement_id"])  # unknown agreement skips the currency check
        if agreement and plans[agreement["plan_id"]]["currency"] != r["currency"]:
            errors.append({"gateway_txn_id": tid, "code": "CURRENCY_MISMATCH"})
    return errors


def reconcile(rows: list[dict], first: date, last: date, agreements: dict, plans: dict) -> dict:
    # expected invoices: every agreement's invoices due in the month, grouped by agreement
    expected: dict[str, list[dict]] = defaultdict(list)
    for a in agreements.values():
        expected[a["id"]] = invoices_between(a, plans[a["plan_id"]], first, last)

    # link each SETTLED row on its own (earliest-due candidate within 1 minor unit; already-linked
    # invoices still count), so the outcome cannot depend on row order
    links: list[tuple[str, int] | None] = []
    for r in rows:
        cands = [] if r["status"] != "SETTLED" else [
            inv for inv in expected.get(r["agreement_id"], []) if abs(inv["total"] - r["amount"]) <= 1
        ]
        best = min(cands, key=lambda inv: (inv["due_date"], inv["invoice_no"]), default=None)
        links.append(best and (r["agreement_id"], best["invoice_no"]))

    linked = Counter(k for k in links if k)
    due = {(aid, inv["invoice_no"]): inv for aid, invs in expected.items() for inv in invs}

    out_rows = []
    for r, key in zip(rows, links):
        if r["status"] == "DECLINED":
            verdict = "DECLINED"
        elif key is None:
            verdict = "MISSING_FROM_LEDGER"
        elif linked[key] > 1:
            verdict = "DOUBLE_CHARGE_RISK"
        elif r["attempted_on"] == due[key]["due_date"]:
            verdict = "MATCHED"
        else:
            verdict = "DUE_DAY_DRIFT"
        out_rows.append({
            "gateway_txn_id": r["gateway_txn_id"],
            "verdict": verdict,
            "invoice": key and {"agreement_id": key[0], "invoice_no": key[1]},
        })

    summary = dict.fromkeys(VERDICTS, 0) | Counter(row["verdict"] for row in out_rows)
    unmatched = sorted(
        (
            {"agreement_id": aid, "invoice_no": no, "due_date": inv["due_date"],
             "total": inv["total"], "currency": inv["currency"]}
            for (aid, no), inv in due.items() if (aid, no) not in linked
        ),
        key=lambda u: (u["due_date"], u["agreement_id"], u["invoice_no"]),
    )
    return {"summary": summary, "rows": out_rows, "unmatched_invoices": unmatched}
