"""
SQLAlchemy database layer for MF Dashboard.
Provides ORM models, session management, and CRUD utilities.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Column, String, Float, Integer, Date, DateTime, Text, Boolean,
    create_engine, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    declarative_base, relationship, sessionmaker, Session,
)
from sqlalchemy.pool import StaticPool

from db_config import DATABASE_URL, ENGINE_PARAMS


Base = declarative_base()


class Portfolio(Base):
    __tablename__ = "portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, default="My Portfolio")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, default=True)

    schemes = relationship("Scheme", back_populates="portfolio", cascade="all, delete-orphan")
    cas_files = relationship("CasFile", back_populates="portfolio", cascade="all, delete-orphan")
    settings = relationship("PortfolioSettings", back_populates="portfolio", uselist=False, cascade="all, delete-orphan")


class PortfolioSettings(Base):
    __tablename__ = "portfolio_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, unique=True)

    equity_ltcg_rate = Column(Float, default=0.125)
    equity_ltcg_exemption = Column(Float, default=125_000.0)
    equity_stcg_rate = Column(Float, default=0.20)
    debt_ltcg_rate = Column(Float, default=0.125)
    debt_stcg_slab = Column(Float, default=0.30)
    equity_holding_threshold_days = Column(Integer, default=365)
    debt_holding_threshold_days = Column(Integer, default=730)

    allocation_targets_json = Column(Text, default="{}")
    scheme_overrides_json = Column(Text, default="{}")
    pan_holders_json = Column(Text, default="{}")

    portfolio = relationship("Portfolio", back_populates="settings")


class Scheme(Base):
    __tablename__ = "schemes"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "pan", "folio", "scheme_name", "amc", name="uq_scheme"),
        Index("idx_scheme_portfolio", "portfolio_id"),
        Index("idx_scheme_pan", "pan"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)
    cas_file_id = Column(Integer, ForeignKey("cas_files.id", ondelete="CASCADE"), nullable=True)

    pan = Column(String(20), nullable=False)
    holder = Column(String(255), default="")
    amc = Column(String(255), default="")
    folio = Column(String(100), default="")
    scheme_name = Column(String(500), nullable=False)
    cost = Column(Float, default=0.0)
    market_value = Column(Float, default=0.0)
    nav_date = Column(Date, nullable=True)
    closing_units = Column(Float, default=0.0)
    sip_status = Column(String(50), default="")
    sip_cancel_date = Column(Date, nullable=True)
    source_file = Column(String(500), default="")

    cashflows = relationship("CashFlow", back_populates="scheme", cascade="all, delete-orphan")
    portfolio = relationship("Portfolio", foreign_keys=[portfolio_id], back_populates="schemes")
    cas_file = relationship("CasFile", foreign_keys=[cas_file_id])


class CashFlow(Base):
    __tablename__ = "cashflows"
    __table_args__ = (
        Index("idx_cf_scheme", "scheme_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    scheme_id = Column(Integer, ForeignKey("schemes.id", ondelete="CASCADE"), nullable=False)

    txn_date = Column(Date, nullable=False)
    kind = Column(String(50), nullable=False)
    amount = Column(Float, default=0.0)
    units = Column(Float, default=0.0)
    price = Column(Float, default=0.0)
    description = Column(String(500), default="")

    scheme = relationship("Scheme", back_populates="cashflows")


class CasFile(Base):
    __tablename__ = "cas_files"
    __table_args__ = (
        Index("idx_cas_portfolio", "portfolio_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False)

    filename = Column(String(500), nullable=False)
    email = Column(String(255), default="")
    statement_start = Column(Date, nullable=True)
    statement_end = Column(Date, nullable=True)
    source_filename = Column(String(500), default="")
    parse_warnings_json = Column(Text, default="[]")

    portfolio = relationship("Portfolio", foreign_keys=[portfolio_id], back_populates="cas_files")


_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(
            DATABASE_URL,
            poolclass=StaticPool if "sqlite" in DATABASE_URL else None,
            **ENGINE_PARAMS,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory


def get_db() -> Session:
    return get_session_factory()()


def init_db():
    engine = get_engine()
    Base.metadata.create_all(engine)


def drop_db():
    engine = get_engine()
    Base.metadata.drop_all(engine)


def save_portfolio(
    session: Session,
    name: str,
    parsed_files: list,
    tax_rates: dict,
    allocation_targets: dict,
    scheme_overrides: dict,
    pan_holders: dict,
) -> Portfolio:
    portfolio = Portfolio(name=name)
    session.add(portfolio)
    session.flush()

    settings = PortfolioSettings(
        portfolio_id=portfolio.id,
        equity_ltcg_rate=tax_rates.get("equity_ltcg_rate", 0.125),
        equity_ltcg_exemption=tax_rates.get("equity_ltcg_exemption", 125_000.0),
        equity_stcg_rate=tax_rates.get("equity_stcg_rate", 0.20),
        debt_ltcg_rate=tax_rates.get("debt_ltcg_rate", 0.125),
        debt_stcg_slab=tax_rates.get("debt_stcg_slab", 0.30),
        equity_holding_threshold_days=tax_rates.get("equity_holding_threshold_days", 365),
        debt_holding_threshold_days=tax_rates.get("debt_holding_threshold_days", 730),
        allocation_targets_json=json.dumps(allocation_targets),
        scheme_overrides_json=json.dumps(scheme_overrides),
        pan_holders_json=json.dumps(pan_holders),
    )
    session.add(settings)

    for fname, cas in parsed_files:
        cas_rec = CasFile(
            portfolio_id=portfolio.id,
            filename=fname,
            email=getattr(cas, "email", ""),
            statement_start=getattr(cas, "statement_start", None),
            statement_end=getattr(cas, "statement_end", None),
            source_filename=getattr(cas, "source_filename", ""),
            parse_warnings_json=json.dumps(getattr(cas, "parse_warnings", [])),
        )
        session.add(cas_rec)
        session.flush()

        for s in getattr(cas, "schemes", []):
            scheme = Scheme(
                portfolio_id=portfolio.id,
                cas_file_id=cas_rec.id,
                pan=s.pan,
                holder=s.holder,
                amc=s.amc,
                folio=s.folio,
                scheme_name=s.scheme_name,
                cost=s.cost,
                market_value=s.market_value,
                nav_date=s.nav_date,
                closing_units=s.closing_units,
                sip_status=s.sip_status,
                sip_cancel_date=s.sip_cancel_date,
                source_file=s.source_file,
            )
            session.add(scheme)
            session.flush()

            for t in getattr(s, "cashflows", []):
                cf = CashFlow(
                    scheme_id=scheme.id,
                    txn_date=t.date,
                    kind=t.kind,
                    amount=t.amount,
                    units=t.units,
                    price=t.price,
                    description=t.description,
                )
                session.add(cf)

    session.commit()
    return portfolio


def load_portfolio(session: Session, portfolio_id: int) -> Optional[Portfolio]:
    return session.get(Portfolio, portfolio_id)


def list_portfolios(session: Session) -> list:
    return session.query(Portfolio).order_by(Portfolio.updated_at.desc()).all()


def delete_portfolio(session: Session, portfolio_id: int):
    portfolio = session.get(Portfolio, portfolio_id)
    if portfolio:
        session.delete(portfolio)
        session.commit()


def portfolio_to_session_state(portfolio: Portfolio) -> dict:
    result = {
        "parsed_files": [],
        "pan_holders": {},
        "tax_rates": {},
        "allocation_targets": {},
        "scheme_overrides": {},
    }

    settings = portfolio.settings
    if settings:
        result["tax_rates"] = {
            "equity_ltcg_rate": settings.equity_ltcg_rate,
            "equity_ltcg_exemption": settings.equity_ltcg_exemption,
            "equity_stcg_rate": settings.equity_stcg_rate,
            "debt_ltcg_rate": settings.debt_ltcg_rate,
            "debt_stcg_slab": settings.debt_stcg_slab,
            "equity_holding_threshold_days": settings.equity_holding_threshold_days,
            "debt_holding_threshold_days": settings.debt_holding_threshold_days,
        }
        result["allocation_targets"] = json.loads(settings.allocation_targets_json or "{}")
        result["scheme_overrides"] = json.loads(settings.scheme_overrides_json or "{}")
        result["pan_holders"] = json.loads(settings.pan_holders_json or "{}")

    from cas_parser import ParsedCAS, Scheme as CasScheme, Transaction

    cas_file_map = {}
    for cas_rec in portfolio.cas_files:
        cas = ParsedCAS(
            email=cas_rec.email,
            statement_start=cas_rec.statement_start,
            statement_end=cas_rec.statement_end,
            source_filename=cas_rec.source_filename,
            parse_warnings=json.loads(cas_rec.parse_warnings_json or "[]"),
        )
        cas_file_map[cas_rec.id] = cas
        result["parsed_files"].append((cas_rec.filename, cas))

    for scheme_rec in portfolio.schemes:
        s = CasScheme(
            pan=scheme_rec.pan,
            holder=scheme_rec.holder,
            amc=scheme_rec.amc,
            folio=scheme_rec.folio,
            scheme_name=scheme_rec.scheme_name,
            cost=scheme_rec.cost,
            market_value=scheme_rec.market_value,
            nav_date=scheme_rec.nav_date,
            closing_units=scheme_rec.closing_units,
            sip_status=scheme_rec.sip_status,
            sip_cancel_date=scheme_rec.sip_cancel_date,
            source_file=scheme_rec.source_file,
        )
        if scheme_rec.cas_file_id and scheme_rec.cas_file_id in cas_file_map:
            cas_file_map[scheme_rec.cas_file_id].schemes.append(s)

    return result