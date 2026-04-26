"""
Analytics engine for the v2 dashboard.

Provides:
  - Scheme classification (asset class, sub-category, tax category)
  - FIFO-based realized & unrealized capital gains
  - Tax preview (LTCG / STCG simulator)
  - Asset-class drift vs target allocation
  - SIP health (active / cancelled / regularity)

Tax rates default to FY 2024-25 onwards (post-July 2024 regime). Override via TaxRates.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

from cas_parser import Scheme, Transaction


# ---------------- Tax regime ----------------
# Post-July 23, 2024 capital-gains regime
@dataclass
class TaxRates:
    equity_ltcg_rate: float = 0.125          # 12.5%
    equity_ltcg_exemption: float = 125_000   # ₹1.25L per FY (annual aggregate)
    equity_stcg_rate: float = 0.20           # 20%
    debt_ltcg_rate: float = 0.125            # 12.5% no indexation (post-July 2024)
    debt_stcg_slab: float = 0.30             # user-overridable; default 30%
    equity_holding_threshold_days: int = 365
    debt_holding_threshold_days: int = 730   # 24 months for debt LTCG (FY24-25 onwards)


# ---------------- Scheme classification ----------------
@dataclass
class SchemeCategory:
    asset_class: str        # Equity / Debt / Hybrid / Liquid / Gold / Other
    sub_category: str       # Small Cap, Mid Cap, ELSS, Index, Sectoral, Liquid, etc.
    tax_category: str       # equity / debt (drives tax rates)


# Order matters — first match wins.
_CATEGORY_RULES = [
    # (substrings to look for, asset_class, sub_category, tax_category)
    (["liquid", "overnight", "money market"], "Debt", "Liquid", "debt"),
    (["gold etf", "gold fund", "gold savings"], "Gold", "Gold", "debt"),
    (["silver etf", "silver fund"], "Other", "Silver", "debt"),
    (["arbitrage"], "Hybrid", "Arbitrage", "equity"),
    (["balanced advantage", "dynamic asset", "dynamic allocation"], "Hybrid", "Balanced Advantage", "equity"),
    (["multi asset", "multi-asset"], "Hybrid", "Multi Asset", "equity"),
    (["aggressive hybrid", "equity hybrid"], "Hybrid", "Aggressive Hybrid", "equity"),
    (["conservative hybrid", "debt hybrid"], "Hybrid", "Conservative Hybrid", "debt"),
    (["elss", "tax saver"], "Equity", "ELSS", "equity"),
    (["small cap", "smallcap"], "Equity", "Small Cap", "equity"),
    (["mid cap", "midcap", "mid-cap"], "Equity", "Mid Cap", "equity"),
    (["large cap", "largecap", "bluechip", "blue chip"], "Equity", "Large Cap", "equity"),
    (["large & mid", "large and mid"], "Equity", "Large & Mid Cap", "equity"),
    (["multi cap", "multicap"], "Equity", "Multi Cap", "equity"),
    (["flexi cap", "flexicap"], "Equity", "Flexi Cap", "equity"),
    (["focused"], "Equity", "Focused", "equity"),
    (["value"], "Equity", "Value", "equity"),
    (["contra"], "Equity", "Contra", "equity"),
    (["dividend yield"], "Equity", "Dividend Yield", "equity"),
    (["momentum"], "Equity", "Momentum/Strategy", "equity"),
    (["quality", "low vol", "low volatility"], "Equity", "Factor/Strategy", "equity"),
    (["pharma", "healthcare"], "Equity", "Sectoral - Pharma", "equity"),
    (["banking", "financial services", "psu bank"], "Equity", "Sectoral - Banking", "equity"),
    (["technology", "digital", "it "], "Equity", "Sectoral - Tech", "equity"),
    (["infrastructure", "infra"], "Equity", "Sectoral - Infra", "equity"),
    (["consumption"], "Equity", "Sectoral - Consumption", "equity"),
    (["energy"], "Equity", "Sectoral - Energy", "equity"),
    (["psu", "public sector"], "Equity", "Sectoral - PSU", "equity"),
    (["defence"], "Equity", "Sectoral - Defence", "equity"),
    (["manufacturing"], "Equity", "Sectoral - Manufacturing", "equity"),
    (["midcap 150", "smallcap 250", "nifty 50", "nifty 100", "nifty 500", "sensex", "index"], "Equity", "Index/Passive", "equity"),
    (["etf"], "Equity", "ETF", "equity"),
    (["nasdaq", "s&p 500", "international", "global", "world", "us tech"], "Equity", "International", "equity"),
    (["gilt"], "Debt", "Gilt", "debt"),
    (["short duration", "short term"], "Debt", "Short Duration", "debt"),
    (["long duration", "long term"], "Debt", "Long Duration", "debt"),
    (["medium duration"], "Debt", "Medium Duration", "debt"),
    (["dynamic bond", "dynamic debt"], "Debt", "Dynamic Bond", "debt"),
    (["corporate bond", "credit risk", "banking and psu", "psu debt"], "Debt", "Corporate Bond", "debt"),
    (["floater", "floating rate"], "Debt", "Floater", "debt"),
    (["ultra short"], "Debt", "Ultra Short", "debt"),
    (["low duration"], "Debt", "Low Duration", "debt"),
    (["income", "debt", "bond"], "Debt", "Bond/Income", "debt"),
]


def categorize_scheme(scheme_name: str) -> SchemeCategory:
    """Classify a scheme by its name. Returns asset_class, sub_category, tax_category."""
    n = scheme_name.lower()
    for keys, ac, sc, tc in _CATEGORY_RULES:
        if any(k in n for k in keys):
            return SchemeCategory(asset_class=ac, sub_category=sc, tax_category=tc)
    # Default to equity (most schemes in Indian retail portfolios are equity)
    return SchemeCategory(asset_class="Equity", sub_category="Other Equity", tax_category="equity")


# ---------------- FIFO Capital Gains ----------------
@dataclass
class GainsLot:
    """A matched purchase-sale lot resulting in realized capital gain."""
    buy_date: date
    sell_date: date
    units: float
    buy_price: float
    sell_price: float
    cost: float           # units * buy_price
    proceeds: float       # units * sell_price
    gain: float           # proceeds - cost
    holding_days: int
    is_long_term: bool
    tax_category: str     # equity / debt
    scheme: str
    pan: str
    holder: str
    amc: str

    @property
    def fy(self) -> str:
        """Indian financial year of the sale (Apr–Mar)."""
        d = self.sell_date
        if d.month >= 4:
            return f"{d.year}-{(d.year + 1) % 100:02d}"
        return f"{d.year - 1}-{d.year % 100:02d}"


@dataclass
class UnrealizedLot:
    """A still-held purchase lot with current unrealized gain at the latest NAV."""
    buy_date: date
    units: float
    buy_price: float
    cost: float
    current_value: float
    gain: float
    holding_days: int
    is_long_term: bool
    tax_category: str
    scheme: str
    pan: str
    holder: str
    amc: str


def _holding_days(buy_d: date, sell_d: date) -> int:
    return (sell_d - buy_d).days


def _is_long_term(holding_days: int, tax_category: str, rates: TaxRates) -> bool:
    threshold = rates.equity_holding_threshold_days if tax_category == "equity" else rates.debt_holding_threshold_days
    return holding_days > threshold


def compute_capital_gains(
    scheme: Scheme,
    rates: Optional[TaxRates] = None,
    as_of: Optional[date] = None,
) -> Tuple[List[GainsLot], List[UnrealizedLot]]:
    """FIFO-match purchases against sales for one scheme.

    Returns (realized_lots, unrealized_lots).
    Sales = redemption + switch_out + shift_out (all are taxable events).
    Buys = purchase + switch_in + shift_in.
    Stamp duty and STT are added to cost basis proportionally to the buy they pair with.
    """
    rates = rates or TaxRates()
    cat = categorize_scheme(scheme.scheme_name)
    as_of = as_of or scheme.nav_date or date.today()

    # Sort transactions chronologically
    txns = sorted(scheme.cashflows, key=lambda t: t.date)

    # FIFO queue of remaining lots: each entry = [buy_date, units_left, buy_price]
    lots = deque()
    realized: List[GainsLot] = []

    # Track stamp duty / STT to distribute across same-day buys (small-money, simplification)
    pending_costs_by_date: Dict[date, float] = defaultdict(float)
    for t in txns:
        if t.kind in ("stamp_duty", "stt"):
            pending_costs_by_date[t.date] += t.amount

    for t in txns:
        if t.kind in ("purchase", "switch_in", "shift_in"):
            if t.units > 0:
                # Add same-day stamp duty to this lot's cost basis
                bonus = pending_costs_by_date.pop(t.date, 0.0)
                effective_price = t.price + (bonus / t.units if t.units else 0)
                lots.append([t.date, t.units, effective_price])
            else:
                # Edge case: amount but no units — derive at face price 1
                # (rare; happens for stamp duty without paired buy)
                pass
        elif t.kind in ("redemption", "switch_out", "shift_out"):
            sell_units = t.units
            sell_price = t.price
            if sell_units <= 0 or sell_price <= 0:
                continue
            remaining = sell_units
            while remaining > 1e-6 and lots:
                lot = lots[0]
                buy_date, lot_units, buy_price = lot
                consumed = min(remaining, lot_units)
                cost = consumed * buy_price
                proceeds = consumed * sell_price
                gain = proceeds - cost
                hd = _holding_days(buy_date, t.date)
                realized.append(GainsLot(
                    buy_date=buy_date,
                    sell_date=t.date,
                    units=consumed,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    cost=cost,
                    proceeds=proceeds,
                    gain=gain,
                    holding_days=hd,
                    is_long_term=_is_long_term(hd, cat.tax_category, rates),
                    tax_category=cat.tax_category,
                    scheme=scheme.scheme_name,
                    pan=scheme.pan,
                    holder=scheme.holder,
                    amc=scheme.amc,
                ))
                lot[1] -= consumed
                remaining -= consumed
                if lot[1] < 1e-6:
                    lots.popleft()

    # Remaining lots are unrealized — value at latest NAV
    # NAV = market_value / closing_units when both available
    nav = (
        scheme.market_value / scheme.closing_units
        if scheme.closing_units and scheme.market_value
        else (scheme.cashflows[-1].price if scheme.cashflows else 0)
    )
    unrealized: List[UnrealizedLot] = []
    for lot in lots:
        buy_date, lot_units, buy_price = lot
        if lot_units < 1e-6:
            continue
        cost = lot_units * buy_price
        current_value = lot_units * nav
        gain = current_value - cost
        hd = _holding_days(buy_date, as_of)
        unrealized.append(UnrealizedLot(
            buy_date=buy_date,
            units=lot_units,
            buy_price=buy_price,
            cost=cost,
            current_value=current_value,
            gain=gain,
            holding_days=hd,
            is_long_term=_is_long_term(hd, cat.tax_category, rates),
            tax_category=cat.tax_category,
            scheme=scheme.scheme_name,
            pan=scheme.pan,
            holder=scheme.holder,
            amc=scheme.amc,
        ))
    return realized, unrealized


# ---------------- Tax computation ----------------
@dataclass
class TaxSummary:
    fy: str
    equity_stcg: float = 0.0
    equity_ltcg_gross: float = 0.0     # before exemption
    equity_ltcg_taxable: float = 0.0   # after exemption
    debt_stcg: float = 0.0
    debt_ltcg: float = 0.0
    equity_stcg_tax: float = 0.0
    equity_ltcg_tax: float = 0.0
    debt_stcg_tax: float = 0.0
    debt_ltcg_tax: float = 0.0
    total_tax: float = 0.0
    total_gain: float = 0.0


def compute_tax_for_lots(
    lots: List[GainsLot],
    rates: Optional[TaxRates] = None,
    debt_slab_rate: Optional[float] = None,
) -> Dict[str, TaxSummary]:
    """Aggregate realized lots by FY and compute tax. Returns {fy: TaxSummary}."""
    rates = rates or TaxRates()
    if debt_slab_rate is not None:
        rates.debt_stcg_slab = debt_slab_rate

    by_fy: Dict[str, TaxSummary] = defaultdict(lambda: TaxSummary(fy=""))
    for lot in lots:
        s = by_fy[lot.fy]
        s.fy = lot.fy
        s.total_gain += lot.gain
        if lot.tax_category == "equity":
            if lot.is_long_term:
                s.equity_ltcg_gross += lot.gain
            else:
                s.equity_stcg += lot.gain
        else:
            if lot.is_long_term:
                s.debt_ltcg += lot.gain
            else:
                s.debt_stcg += lot.gain

    for fy, s in by_fy.items():
        # Equity LTCG: exemption applies once per FY
        s.equity_ltcg_taxable = max(0.0, s.equity_ltcg_gross - rates.equity_ltcg_exemption)
        s.equity_ltcg_tax = max(0.0, s.equity_ltcg_taxable * rates.equity_ltcg_rate)
        s.equity_stcg_tax = max(0.0, s.equity_stcg * rates.equity_stcg_rate)
        s.debt_ltcg_tax = max(0.0, s.debt_ltcg * rates.debt_ltcg_rate)
        s.debt_stcg_tax = max(0.0, s.debt_stcg * rates.debt_stcg_slab)
        s.total_tax = (
            s.equity_ltcg_tax + s.equity_stcg_tax
            + s.debt_ltcg_tax + s.debt_stcg_tax
        )
    return dict(by_fy)


def simulate_full_redemption_tax(
    schemes: List[Scheme],
    rates: Optional[TaxRates] = None,
    debt_slab_rate: Optional[float] = None,
    as_of: Optional[date] = None,
) -> Tuple[float, float, float, TaxSummary]:
    """Pretend we redeem ALL holdings today. Returns (total_proceeds, total_tax, net_proceeds, summary)."""
    rates = rates or TaxRates()
    if debt_slab_rate is not None:
        rates.debt_stcg_slab = debt_slab_rate
    today = as_of or date.today()
    all_unrealized: List[UnrealizedLot] = []
    for s in schemes:
        _, ur = compute_capital_gains(s, rates, today)
        all_unrealized.extend(ur)

    summary = TaxSummary(fy="hypothetical")
    total_proceeds = 0.0
    for lot in all_unrealized:
        total_proceeds += lot.current_value
        summary.total_gain += lot.gain
        if lot.tax_category == "equity":
            if lot.is_long_term:
                summary.equity_ltcg_gross += lot.gain
            else:
                summary.equity_stcg += lot.gain
        else:
            if lot.is_long_term:
                summary.debt_ltcg += lot.gain
            else:
                summary.debt_stcg += lot.gain

    summary.equity_ltcg_taxable = max(0.0, summary.equity_ltcg_gross - rates.equity_ltcg_exemption)
    summary.equity_ltcg_tax = max(0.0, summary.equity_ltcg_taxable * rates.equity_ltcg_rate)
    summary.equity_stcg_tax = max(0.0, summary.equity_stcg * rates.equity_stcg_rate)
    summary.debt_ltcg_tax = max(0.0, summary.debt_ltcg * rates.debt_ltcg_rate)
    summary.debt_stcg_tax = max(0.0, summary.debt_stcg * rates.debt_stcg_slab)
    summary.total_tax = (
        summary.equity_ltcg_tax + summary.equity_stcg_tax
        + summary.debt_ltcg_tax + summary.debt_stcg_tax
    )
    return total_proceeds, summary.total_tax, total_proceeds - summary.total_tax, summary


# ---------------- Asset allocation drift ----------------
@dataclass
class DriftRow:
    asset_class: str
    current_value: float
    current_pct: float
    target_pct: float
    drift_pct: float          # current - target (positive = overweight)
    rebalance_amount: float   # +ve = need to buy more, -ve = need to sell


def compute_drift(
    schemes: List[Scheme],
    targets: Dict[str, float],
) -> List[DriftRow]:
    """targets: {'Equity': 70, 'Debt': 20, 'Gold': 10, ...} as percentages."""
    by_class: Dict[str, float] = defaultdict(float)
    total = 0.0
    for s in schemes:
        cat = categorize_scheme(s.scheme_name)
        by_class[cat.asset_class] += s.market_value
        total += s.market_value
    if total <= 0:
        return []

    classes = sorted(set(list(by_class.keys()) + list(targets.keys())))
    rows = []
    for c in classes:
        cur_val = by_class.get(c, 0.0)
        cur_pct = cur_val / total * 100 if total else 0
        tgt_pct = targets.get(c, 0.0)
        drift = cur_pct - tgt_pct
        rebalance = (tgt_pct / 100 * total) - cur_val  # +ve means buy more
        rows.append(DriftRow(
            asset_class=c, current_value=cur_val, current_pct=cur_pct,
            target_pct=tgt_pct, drift_pct=drift, rebalance_amount=rebalance,
        ))
    return rows


# ---------------- SIP health ----------------
@dataclass
class SipInfo:
    pan: str
    holder: str
    amc: str
    scheme: str
    status: str               # active / cancelled / unknown
    cancel_date: Optional[date]
    typical_amount: float
    last_sip_date: Optional[date]
    days_since_last: Optional[int]
    sip_count: int


def analyze_sips(schemes: List[Scheme], as_of: Optional[date] = None) -> List[SipInfo]:
    """Identify SIP cadence per scheme and report health."""
    as_of = as_of or date.today()
    out = []
    for s in schemes:
        sip_txns = [
            t for t in s.cashflows
            if t.kind == "purchase"
            and ("sip" in t.description.lower() or "systematic" in t.description.lower())
        ]
        if not sip_txns:
            continue
        amounts = [t.amount for t in sip_txns]
        typical = sorted(amounts)[len(amounts) // 2] if amounts else 0
        last = max(t.date for t in sip_txns) if sip_txns else None
        out.append(SipInfo(
            pan=s.pan,
            holder=s.holder,
            amc=s.amc,
            scheme=s.scheme_name,
            status=s.sip_status or "unknown",
            cancel_date=s.sip_cancel_date,
            typical_amount=typical,
            last_sip_date=last,
            days_since_last=(as_of - last).days if last else None,
            sip_count=len(sip_txns),
        ))
    return out


# ---------------- Helper: holder canonicalization ----------------
def canonical_holder_per_pan(schemes: List[Scheme]) -> Dict[str, str]:
    """Pick the most likely primary holder name for each PAN, preferring names unique to that PAN."""
    from collections import Counter
    pan_to_names = defaultdict(list)
    for s in schemes:
        if s.holder and s.holder.upper() not in {"KFINTECH", "CAMS", "REGISTRAR"}:
            pan_to_names[s.pan].append(s.holder)
    out = {}
    for pan, names in pan_to_names.items():
        other = set()
        for p, hs in pan_to_names.items():
            if p != pan:
                other.update(hs)
        unique = [n for n in names if n not in other]
        pool = unique if unique else names
        cnt = Counter(pool)
        if cnt:
            top_count = cnt.most_common(1)[0][1]
            cands = [n for n, c in cnt.items() if c == top_count]
            out[pan] = max(cands, key=len)
        else:
            out[pan] = ""
    return out
