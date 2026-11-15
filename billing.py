"""Agreement billing schedule, computed on request. All amounts are integer minor units (halalas / fils).

Rounding: half-up to a whole minor unit, exactly twice per invoice and never in between:
once for the prorated amount (FIRST only), once for VAT. Never round in major units.
"""

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal


def due_date_in(year: int, month: int, due_day: int) -> date:
    """Due date for that month: min(due_day, days in month). The due_day anchor itself never changes,
    so 31 -> 30 Apr, 28 Feb (29 in leap years), and back to 31 Mar."""
    return date(year, month, min(due_day, calendar.monthrange(year, month)[1]))


def month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def due_at(idx: int, due_day: int) -> date:
    year, m0 = divmod(idx, 12)
    return due_date_in(year, m0 + 1, due_day)


def prorate(monthly: int, days: int, cycle_days: int) -> int:
    """round_half_up(monthly * days / cycle_days), in exact integer math (no float, no early rounding)."""
    return (2 * monthly * days + cycle_days) // (2 * cycle_days)


def vat_of(net: int, rate: Decimal) -> int:
    return int((Decimal(net) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def invoice(no: int, due: date, kind: str, proration: dict | None, net: int, plan: dict) -> dict:
    vat = vat_of(net, plan["vat_rate"])  # joining fee (if any) is inside the VAT base
    return {"invoice_no": no, "due_date": due, "kind": kind, "proration": proration,
            "net": net, "vat": vat, "total": net + vat, "currency": plan["currency"]}


def invoices_between(agreement: dict, plan: dict, frm: date, to: date) -> list[dict]:
    """Invoices whose due date is in [frm, to]; invoice_no is the position in the whole schedule.

    1 FIRST    due on start. N = first due date strictly after start, P = due date one month before N.
               net = round(monthly * (N-start)/(N-P)) + joining_fee. Start on a due date -> full monthly.
    2 MONTHLY  due on N, full monthly. 3 due the month after, and so on.
    """
    start, due_day = agreement["start_date"], agreement["due_day"]
    n_idx = month_index(start)
    if due_at(n_idx, due_day) <= start:  # strictly after start
        n_idx += 1
    n, p = due_at(n_idx, due_day), due_at(n_idx - 1, due_day)

    out = []
    if frm <= start <= to:
        days, cycle = (n - start).days, (n - p).days
        net = prorate(plan["monthly"], days, cycle) + plan["joining_fee"]
        out.append(invoice(1, start, "FIRST", {"days": days, "cycle_days": cycle}, net, plan))

    for idx in range(max(n_idx, month_index(frm)), month_index(to) + 1):
        due = due_at(idx, due_day)
        if frm <= due <= to:
            out.append(invoice(idx - n_idx + 2, due, "MONTHLY", None, plan["monthly"], plan))
    return out
