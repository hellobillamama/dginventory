"""SQLAlchemy engine and models. SQLite by default; DATABASE_URL for Postgres."""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            import streamlit as st

            url = st.secrets.get("DATABASE_URL")
        except Exception:
            url = None
    if not url:
        return "sqlite:///dg_inventory.db"
    url = str(url).strip()
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://") :]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    return url


def _make_engine() -> Engine:
    url = _database_url()
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)

    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    engine = _make_engine()
    Base.metadata.create_all(engine)
    _ensure_indexes(engine)
    _ensure_counter(engine)
    _ensure_karigar_column(engine)
    return engine


def reset_engine_cache() -> None:
    get_engine.cache_clear()


def SessionLocal():
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)()


class Base(DeclarativeBase):
    pass


class BOM(Base):
    __tablename__ = "bom"
    __table_args__ = (
        UniqueConstraint("style", "material", name="uq_bom_style_material"),
        Index("ix_bom_style", "style"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    style: Mapped[str] = mapped_column(String(255), nullable=False)
    material: Mapped[str] = mapped_column(String(512), nullable=False)
    qty_per_unit: Mapped[float] = mapped_column(Float, nullable=False)


class Inventory(Base):
    __tablename__ = "inventory"

    material: Mapped[str] = mapped_column(String(512), primary_key=True)
    stock_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unit: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    item_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)


class Counter(Base):
    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    txn_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    style: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    po_no: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    qty: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    designer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    karigar_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    lines: Mapped[list["TransactionLine"]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )


class TransactionLine(Base):
    __tablename__ = "transaction_lines"
    __table_args__ = (Index("ix_txn_lines_material", "material"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    transaction_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("transactions.id"), nullable=False, index=True
    )
    material: Mapped[str] = mapped_column(String(512), nullable=False)
    qty_deducted: Mapped[float] = mapped_column(Float, nullable=False)

    transaction: Mapped[Transaction] = relationship(back_populates="lines")


def _ensure_indexes(engine: Engine) -> None:
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_bom_style ON bom (style)"))
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_txn_lines_material ON transaction_lines (material)")
            )
        else:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_bom_style ON bom (style)"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_txn_lines_material ON transaction_lines (material)"
                )
            )


def _ensure_counter(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO counters (name, value) SELECT 'item_id', 1 "
                "WHERE NOT EXISTS (SELECT 1 FROM counters WHERE name = 'item_id')"
            )
        )


def _ensure_karigar_column(engine: Engine) -> None:
    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            cols = {row[1] for row in conn.execute(text("PRAGMA table_info(transactions)")).fetchall()}
        else:
            cols = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'transactions'"
                    )
                ).fetchall()
            }
        if "karigar_name" not in cols:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN karigar_name VARCHAR(255)"))


def dialect_name() -> str:
    return get_engine().dialect.name
