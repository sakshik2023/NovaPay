"""Fake acquiring bank / UPI switch.  Deterministic so the project can be demoed and tested.

Test cards (use any future expiry and any 3-digit CVV):
  4111 1111 1111 1111  Visa        -> success
  5555 5555 5555 4444  Mastercard  -> success
  4000 0000 0000 3220  Visa        -> 3-D Secure, OTP = 123456
  4000 0000 0000 9995  Visa        -> declined: insufficient funds
  4000 0000 0000 0069  Visa        -> declined: card expired
  4000 0000 0000 0002  Visa        -> declined: generic
  4000 0000 0000 0119  Visa        -> blocked by NovaPay fraud engine
UPI: success@upi -> success | failure@upi -> declined | timeout@upi -> bank timeout
"""
import re
import uuid
from dataclasses import dataclass
from datetime import date

TEST_OTP = "123456"
BLOCKED_CARDS = {"4000000000000119"}

CARD_OUTCOMES = {
    "4000000000009995": ("INSUFFICIENT_FUNDS", "Card declined: insufficient funds"),
    "4000000000000069": ("CARD_EXPIRED", "Card declined: card has expired"),
    "4000000000000002": ("CARD_DECLINED", "Card declined by issuing bank"),
}
OTP_CARDS = {"4000000000003220"}


@dataclass
class BankResult:
    status: str            # success | otp_required | failed
    code: str = ""
    reason: str = ""
    reference: str = ""


def luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def detect_network(number: str) -> str:
    if number.startswith("4"):
        return "visa"
    if re.match(r"^(5[1-5]|2[2-7])", number):
        return "mastercard"
    if re.match(r"^(34|37)", number):
        return "amex"
    if re.match(r"^(60|65|81|82|508)", number):
        return "rupay"
    return "card"


def normalise_card(number: str) -> str:
    return re.sub(r"[\s-]", "", number or "")


def validate_card(number: str, expiry: str, cvv: str):
    """Returns (ok, field, message). expiry is MM/YY or MM/YYYY."""
    number = normalise_card(number)
    if not number.isdigit() or not (13 <= len(number) <= 19) or not luhn_valid(number):
        return False, "card_number", "Enter a valid card number"
    m = re.match(r"^(\d{1,2})\s*/\s*(\d{2}|\d{4})$", (expiry or "").strip())
    if not m:
        return False, "expiry", "Enter expiry as MM/YY"
    month, year = int(m.group(1)), int(m.group(2))
    year += 2000 if year < 100 else 0
    if not 1 <= month <= 12:
        return False, "expiry", "Invalid expiry month"
    today = date.today()
    if (year, month) < (today.year, today.month):
        return False, "expiry", "Card has expired"
    need = 4 if detect_network(number) == "amex" else 3
    if not (cvv or "").isdigit() or len(cvv) != need:
        return False, "cvv", f"CVV must be {need} digits"
    return True, "", ""


def validate_vpa(vpa: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9._-]{2,40}@[A-Za-z]{2,20}$", (vpa or "").strip()))


def _ref(prefix="BNK"):
    return f"{prefix}{uuid.uuid4().hex[:10].upper()}"


def charge_card(number: str) -> BankResult:
    number = normalise_card(number)
    if number in CARD_OUTCOMES:
        code, reason = CARD_OUTCOMES[number]
        return BankResult("failed", code, reason)
    if number in OTP_CARDS:
        return BankResult("otp_required", reference=_ref())
    return BankResult("success", reference=_ref())


def verify_otp(otp: str) -> bool:
    return (otp or "").strip() == TEST_OTP


def charge_upi(vpa: str) -> BankResult:
    vpa = (vpa or "").strip().lower()
    if vpa.startswith("failure"):
        return BankResult("failed", "UPI_DECLINED", "UPI request declined by customer's bank")
    if vpa.startswith("timeout"):
        return BankResult("failed", "BANK_TIMEOUT", "Bank did not respond in time")
    return BankResult("success", reference=_ref("UPI"))
