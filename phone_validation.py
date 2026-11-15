"""Phone rules for AE, SA and BH mobiles, exactly as the spec's country table defines them.

Public API:
    member_phone(raw, country) -> E.164   rules for POST /members
    lookup_phone(raw, country) -> E.164   rules for GET /members/lookup
All failures raise PhoneError(status, code, **extra). No web framework imports, so it is reusable.
"""

import re

DIAL = {"AE": "971", "SA": "966", "BH": "973"}
# national mobile shape per country; AE/SA have a trunk 0, BH has none
NATIONAL = {"AE": re.compile(r"05\d{8}"), "SA": re.compile(r"05\d{8}"), "BH": re.compile(r"3\d{7}")}
# what follows the dial code in E.164
SUBSCRIBER = {"AE": re.compile(r"5\d{8}"), "SA": re.compile(r"5\d{8}"), "BH": re.compile(r"3\d{7}")}


class PhoneError(Exception):
    def __init__(self, status: int, code: str, **extra):
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


def fail(code: str, **extra):
    raise PhoneError(422, code, **extra)


def clean(raw: str) -> str:
    """Remove spaces, hyphens and round brackets; only digits and one leading + may remain."""
    cleaned = re.sub(r"[ \-()]", "", raw)
    if not re.fullmatch(r"\+?\d+", cleaned):
        fail("INVALID_PHONE", phone=raw)
    return cleaned


def international(cleaned: str) -> tuple[str, str] | None:
    """'+<dial><mobile>' or '<dial><mobile>' -> (E.164, country). 00-prefix never matches."""
    digits = cleaned.removeprefix("+")
    for country, dial in DIAL.items():
        if digits.startswith(dial) and SUBSCRIBER[country].fullmatch(digits[len(dial):]):
            return "+" + digits, country
    return None


def national(cleaned: str, country: str) -> str | None:
    """National mobile of that country -> E.164, else None."""
    if NATIONAL[country].fullmatch(cleaned):
        return "+" + DIAL[country] + cleaned.removeprefix("0")
    return None


def member_phone(raw: str, country: str) -> str:
    """POST /members: international may be any of the three countries (foreign SIMs);
    national is read in the member's own country."""
    cleaned = clean(raw)
    intl = international(cleaned)
    if intl:
        return intl[0]
    return national(cleaned, country) or fail("INVALID_PHONE", phone=raw)


def lookup_phone(raw: str, country: str | None) -> str:
    """GET /members/lookup: national needs a country; international must agree with it."""
    cleaned = clean(raw)
    intl = international(cleaned)
    if intl:
        if country and country != intl[1]:
            fail("PHONE_COUNTRY_MISMATCH", phone=raw, country=country)
        return intl[0]
    if country is None:
        if any(national(cleaned, c) for c in DIAL):
            fail("PHONE_NEEDS_COUNTRY", phone=raw)
        fail("INVALID_PHONE", phone=raw)
    return national(cleaned, country) or fail("INVALID_PHONE", phone=raw, country=country)
