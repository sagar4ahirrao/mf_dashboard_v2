"""
Enhanced CAS parser (v2).
Captures full transaction details (amount, units, price) needed for FIFO capital gains.
Preserves switches and lateral shifts as separately labeled transactions so downstream code
can include or exclude them based on the metric (XIRR excludes intra-portfolio movements;
capital-gains FIFO includes switch-outs as sales).
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional, Union

import pdfplumber
from scipy.optimize import brentq


# ---------------- Regex patterns ----------------
DATE_RE = re.compile(r"^(\d{2}-[A-Za-z]{3}-\d{4})\s+(.*)$")
NUM_RE = re.compile(r"\(?-?[\d,]+\.\d+\)?")
FOLIO_RE = re.compile(r"Folio No:\s*([\w\s/]+?)\s+PAN:\s*([A-Z0-9]{10})")
SCHEME_RE = re.compile(r"^([A-Z0-9]+)-(.+?)\s*-\s*ISIN:")
CLOSE_RE = re.compile(
    r"Closing Unit Balance:\s*([\d,.]+)\s+NAV on\s+([\d\-A-Za-z]+):\s*INR\s*([\d,.]+)\s+"
    r"Total Cost Value:\s*([\d,.]+)\s+Market Value on\s+[\d\-A-Za-z]+:\s*INR\s*([\d,.]+)"
)
HOLDER_RE = re.compile(r"^[A-Z][A-Z\s.]{5,}$")
EMAIL_RE = re.compile(r"Email Id:\s*(\S+@\S+)")
PERIOD_RE = re.compile(r"(\d{2}-[A-Za-z]{3}-\d{4})\s+To\s+(\d{2}-[A-Za-z]{3}-\d{4})")

AMC_NAMES = sorted({
    "ICICI Prudential Mutual Fund", "SBI Mutual Fund", "Tata Mutual Fund",
    "Mirae Asset Mutual Fund", "MOTILAL OSWAL MUTUAL FUND",
    "Quant MF", "Nippon India Mutual Fund",
    "HDFC Mutual Fund", "Axis Mutual Fund", "Kotak Mahindra Mutual Fund",
    "Aditya Birla Sun Life Mutual Fund", "DSP Mutual Fund",
    "Franklin Templeton Mutual Fund", "UTI Mutual Fund", "Bandhan Mutual Fund",
    "Edelweiss Mutual Fund", "PGIM India Mutual Fund", "Invesco Mutual Fund",
    "Sundaram Mutual Fund", "Canara Robeco Mutual Fund", "JM Financial Mutual Fund",
    "Mahindra Manulife Mutual Fund", "PPFAS Mutual Fund",
    "Parag Parikh Mutual Fund", "Baroda BNP Paribas Mutual Fund",
    "WhiteOak Capital Mutual Fund", "HSBC Mutual Fund",
    "Bank of India Mutual Fund", "Quantum Mutual Fund", "Navi Mutual Fund",
    "ITI Mutual Fund", "Trust Mutual Fund", "Samco Mutual Fund",
    "NJ Mutual Fund", "Helios Mutual Fund", "Old Bridge Mutual Fund",
    "Zerodha Mutual Fund", "Groww Mutual Fund", "Bajaj Finserv Mutual Fund",
    "Shriram Mutual Fund", "Union Mutual Fund", "IIFL Mutual Fund",
    "Taurus Mutual Fund", "LIC Mutual Fund", "IDBI Mutual Fund",
    "Indiabulls Mutual Fund",
}, key=len, reverse=True)


# ---------------- Data classes ----------------
@dataclass
class Transaction:
    date: date
    kind: str            # purchase, redemption, stamp_duty, stt, switch_in, switch_out, shift_in, shift_out
    amount: float        # always positive — direction implied by kind
    units: float = 0.0   # positive number; 0 if not applicable
    price: float = 0.0   # NAV per unit at the time
    description: str = ""

    @property
    def signed_amount(self) -> float:
        """Sign convention for XIRR: outflow=negative, inflow=positive."""
        if self.kind in ("purchase", "stamp_duty", "stt", "switch_in", "shift_in"):
            return -abs(self.amount)
        if self.kind in ("redemption", "switch_out", "shift_out"):
            return abs(self.amount)
        return 0.0


@dataclass
class Scheme:
    pan: str = ""
    holder: str = ""
    amc: str = ""
    folio: str = ""
    scheme_name: str = ""
    cashflows: List[Transaction] = field(default_factory=list)
    cost: float = 0.0           # from CAS closing summary (cost basis of units still held)
    market_value: float = 0.0   # from CAS closing summary
    nav_date: Optional[date] = None
    closing_units: float = 0.0  # from CAS closing summary
    sip_status: str = ""        # "active", "cancelled", "" (none detected)
    sip_cancel_date: Optional[date] = None
    source_file: str = ""


@dataclass
class ParsedCAS:
    email: str = ""
    statement_start: Optional[date] = None
    statement_end: Optional[date] = None
    schemes: List[Scheme] = field(default_factory=list)
    source_filename: str = ""
    parse_warnings: List[str] = field(default_factory=list)


# ---------------- Helpers ----------------
def _to_float(s: str) -> float:
    s = s.strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "")
    return -float(s) if neg else float(s)


def _classify(desc: str) -> Optional[str]:
    d = desc.lower().replace("*", "").strip()
    annotations = (
        "address updated", "kyc", "nominee", "can data updation",
        "bank mandate", "nct ", "fatca", "registration of",
        "change / regn", "updation of",
    )
    if any(a in d for a in annotations):
        return None
    if "stamp duty" in d:
        return "stamp_duty"
    if "stt paid" in d:
        return "stt"
    if "redemption" in d or "redeem" in d:
        return "redemption"
    if "switch-in" in d or "switch in" in d:
        return "switch_in"
    if "switch-out" in d or "switch out" in d:
        return "switch_out"
    if "lateral shift in" in d:
        return "shift_in"
    if "lateral shift out" in d:
        return "shift_out"
    purchase_keys = (
        "purchase", "systematic investment", "sys. investment",
        "sys investment", "isip", "additional purchase", "initial purchase",
    )
    if any(k in d for k in purchase_keys):
        return "purchase"
    return None


def _detect_sip_event(desc: str) -> Optional[str]:
    d = desc.lower().replace("*", "").strip()
    if "sipregistered" in d:
        return "registered"
    if "sipcancelled" in d or ("sip" in d and "cancelled" in d):
        return "cancelled"
    if "cancelled ceased" in d:
        return "cancelled"
    return None


# ---------------- Parser ----------------
def parse_cas(
    pdf_source: Union[str, bytes, io.BytesIO],
    password: Optional[str] = None,
    source_filename: str = "",
) -> ParsedCAS:
    """Parse a CAS PDF. Pass bytes, file-like, or path. Optional password (PAN)."""
    if isinstance(pdf_source, bytes):
        pdf_source = io.BytesIO(pdf_source)

    open_kwargs = {"password": password} if password else {}
    parsed = ParsedCAS(source_filename=source_filename)

    try:
        with pdfplumber.open(pdf_source, **open_kwargs) as pdf:
            pages = []
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
            full_text = "\n".join(pages)
    except Exception as e:
        parsed.parse_warnings.append(f"Failed to open PDF: {e}")
        return parsed

    if not full_text:
        parsed.parse_warnings.append("No text extracted (PDF may be scanned)")
        return parsed

    lines = full_text.split("\n")

    # Email and period
    for line in lines[:80]:
        if not parsed.email:
            m = EMAIL_RE.search(line)
            if m:
                parsed.email = m.group(1).strip().rstrip(".,;:")
        if not parsed.statement_start:
            m = PERIOD_RE.search(line)
            if m:
                try:
                    parsed.statement_start = datetime.strptime(m.group(1), "%d-%b-%Y").date()
                    parsed.statement_end = datetime.strptime(m.group(2), "%d-%b-%Y").date()
                except ValueError:
                    pass

    schemes_dict: dict = {}
    state = {"amc": None, "folio": None, "pan": None, "holder": None, "key": None}

    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue

        # 1) AMC header
        amc_match = next(
            (a for a in AMC_NAMES if stripped == a or stripped.upper() == a.upper()),
            None,
        )
        if amc_match:
            state["amc"] = amc_match
            state["key"] = None
            continue

        # 2) Folio header
        m = FOLIO_RE.search(stripped)
        if m:
            state["folio"] = m.group(1).strip()
            state["pan"] = m.group(2).strip()
            state["key"] = None
            for j in range(i + 1, min(i + 5, len(lines))):
                cand = lines[j].strip()
                if (
                    HOLDER_RE.match(cand)
                    and "ISIN" not in cand
                    and "Folio" not in cand
                    and "KFINTECH" not in cand
                    and "CAMS" not in cand
                    and "MUTUAL FUND" not in cand
                    and "REGISTRAR" not in cand.upper()
                ):
                    state["holder"] = cand
                    break
            continue

        # 3) Scheme header
        if "ISIN:" in stripped:
            m = SCHEME_RE.match(stripped)
            if m:
                scheme_name = m.group(2).strip()
                key = (state["amc"], state["folio"], scheme_name, state["pan"])
                if key not in schemes_dict:
                    schemes_dict[key] = Scheme(
                        pan=state["pan"] or "",
                        holder=state["holder"] or "",
                        amc=state["amc"] or "",
                        folio=state["folio"] or "",
                        scheme_name=scheme_name,
                        source_file=source_filename,
                    )
                state["key"] = key
                continue

        # 4) Closing balance
        m = CLOSE_RE.search(stripped)
        if m and state["key"]:
            sch = schemes_dict[state["key"]]
            try:
                sch.closing_units = float(m.group(1).replace(",", ""))
                sch.cost = float(m.group(4).replace(",", ""))
                sch.market_value = float(m.group(5).replace(",", ""))
                sch.nav_date = datetime.strptime(m.group(2), "%d-%b-%Y").date()
            except (ValueError, AttributeError):
                pass
            continue

        # 5) Transaction line — DATE THEN [text] [amount] [units] [price] [balance]
        m = DATE_RE.match(stripped)
        if not (m and state["key"]):
            continue
        try:
            dt = datetime.strptime(m.group(1), "%d-%b-%Y").date()
        except ValueError:
            continue
        rest = m.group(2)

        # SIP status events (no cash flow)
        sip_event = _detect_sip_event(rest)
        if sip_event:
            sch = schemes_dict[state["key"]]
            if sip_event == "registered":
                sch.sip_status = "active"
            elif sip_event == "cancelled":
                sch.sip_status = "cancelled"
                sch.sip_cancel_date = dt
            continue

        kind = _classify(rest)
        if kind is None:
            continue

        nums = NUM_RE.findall(rest)
        if not nums:
            continue
        try:
            amt = abs(_to_float(nums[0]))
        except ValueError:
            continue

        # Units & price (when 4 numbers present: amount, units, price, balance)
        units = 0.0
        price = 0.0
        if len(nums) >= 3:
            try:
                units = abs(_to_float(nums[1]))
                price = abs(_to_float(nums[2]))
            except ValueError:
                pass

        sch = schemes_dict[state["key"]]
        sch.cashflows.append(Transaction(
            date=dt, kind=kind, amount=amt, units=units, price=price,
            description=rest[:120],
        ))

    parsed.schemes = list(schemes_dict.values())
    return parsed


# ---------------- XIRR ----------------
def xnpv(rate: float, flows: list) -> float:
    if rate <= -1:
        return float("inf")
    t0 = flows[0][0]
    return sum(amt / (1 + rate) ** ((d - t0).days / 365.0) for d, amt in flows)


def xirr(flows: list) -> Optional[float]:
    """Compute XIRR for [(date, amount), ...]. Returns None if not solvable."""
    if len(flows) < 2:
        return None
    flows = sorted(flows, key=lambda x: x[0])
    if not (any(a > 0 for _, a in flows) and any(a < 0 for _, a in flows)):
        return None
    for lo, hi in [(-0.999, 10.0), (-0.5, 5.0), (-0.9, 100.0)]:
        try:
            return brentq(lambda r: xnpv(r, flows), lo, hi, xtol=1e-7)
        except ValueError:
            continue
    return None


def scheme_external_flows(scheme: Scheme, terminal_date: Optional[date] = None) -> list:
    """Cash flows for portfolio XIRR — switches and shifts excluded as intra-portfolio."""
    flows = []
    for t in scheme.cashflows:
        if t.kind in ("switch_in", "switch_out", "shift_in", "shift_out"):
            continue
        flows.append((t.date, t.signed_amount))
    if scheme.market_value > 0:
        td = terminal_date or scheme.nav_date or date.today()
        flows.append((td, scheme.market_value))
    return flows
