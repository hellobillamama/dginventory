"""Copy the office SQLite file into a persistent Postgres database (Neon / Supabase)."""

from __future__ import annotations

import os
import re
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from db import (
    BOM,
    Base,
    Counter,
    Inventory,
    Transaction,
    TransactionLine,
    _ensure_counter,
    _ensure_indexes,
    _ensure_karigar_column,
    normalize_database_url,
)

def save_database_url_secret(url: str) -> Path:
    """Write DATABASE_URL into local .streamlit/secrets.toml without dropping other keys."""
    path = ROOT / ".streamlit" / "secrets.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    line = f'DATABASE_URL = "{url.replace(chr(34), "")}"'
    if re.search(r"^DATABASE_URL\s*=", raw, flags=re.M):
        raw = re.sub(r"^DATABASE_URL\s*=.*$", line, raw, count=1, flags=re.M)
    else:
        raw = line + "\n\n" + raw
    path.write_text(raw.rstrip() + "\n", encoding="utf-8")
    return path


def _sqlite_engine() -> Engine:
    path = DEFAULT_SQLITE.as_posix()
    engine = create_engine(f"sqlite:///{path}", future=True)
    return engine


def _pg_engine(url: str) -> Engine:
    engine = create_engine(
        normalize_database_url(url),
        future=True,
        pool_pre_ping=True,
        pool_recycle=280,
    )
    Base.metadata.create_all(engine)
    _ensure_indexes(engine)
    _ensure_counter(engine)
    _ensure_karigar_column(engine)
    return engine


def _chunk(rows: list, size: int = 800):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _reset_pg_sequence(conn, table: str, column: str = "id") -> None:
    conn.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
            f"COALESCE((SELECT MAX({column}) FROM {table}), 1))"
        )
    )


def migrate_sqlite_to_postgres(postgres_url: str) -> dict[str, int]:
    if not DEFAULT_SQLITE.exists():
        raise FileNotFoundError(f"Local database not found: {DEFAULT_SQLITE}")

    src = sessionmaker(bind=_sqlite_engine(), future=True)()
    dest_engine = _pg_engine(postgres_url)
    dest = sessionmaker(bind=dest_engine, future=True)()
    counts: dict[str, int] = {}
    try:
        dest.execute(text("TRUNCATE transaction_lines, transactions, bom, inventory, counters RESTART IDENTITY CASCADE"))
        dest.commit()

        counters = [
            {"name": r.name, "value": r.value}
            for r in src.execute(select(Counter)).scalars()
        ]
        if counters:
            dest.execute(Counter.__table__.insert(), counters)
        counts["counters"] = len(counters)

        inventory = [
            {
                "material": r.material,
                "stock_qty": r.stock_qty,
                "unit": r.unit,
                "item_id": r.item_id,
            }
            for r in src.execute(select(Inventory)).scalars()
        ]
        for batch in _chunk(inventory):
            dest.execute(Inventory.__table__.insert(), batch)
        counts["inventory"] = len(inventory)

        bom = [
            {
                "id": r.id,
                "style": r.style,
                "material": r.material,
                "qty_per_unit": r.qty_per_unit,
            }
            for r in src.execute(select(BOM)).scalars()
        ]
        for batch in _chunk(bom):
            dest.execute(BOM.__table__.insert(), batch)
        counts["bom"] = len(bom)

        txns = [
            {
                "id": r.id,
                "timestamp": r.timestamp,
                "txn_type": r.txn_type,
                "style": r.style,
                "po_no": r.po_no,
                "qty": r.qty,
                "designer_name": r.designer_name,
                "karigar_name": getattr(r, "karigar_name", None),
                "note": r.note,
            }
            for r in src.execute(select(Transaction)).scalars()
        ]
        for batch in _chunk(txns):
            dest.execute(Transaction.__table__.insert(), batch)
        counts["transactions"] = len(txns)

        lines = [
            {
                "id": r.id,
                "transaction_id": r.transaction_id,
                "material": r.material,
                "qty_deducted": r.qty_deducted,
            }
            for r in src.execute(select(TransactionLine)).scalars()
        ]
        for batch in _chunk(lines):
            dest.execute(TransactionLine.__table__.insert(), batch)
        counts["transaction_lines"] = len(lines)

        dest.commit()
        with dest_engine.begin() as conn:
            _reset_pg_sequence(conn, "bom")
            _reset_pg_sequence(conn, "transactions")
            _reset_pg_sequence(conn, "transaction_lines")
        return counts
    except Exception:
        dest.rollback()
        raise
    finally:
        src.close()
        dest.close()
        dest_engine.dispose()


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("Set DATABASE_URL to your Neon/Supabase Postgres connection string, then run this script.")
    result = migrate_sqlite_to_postgres(url)
    print("Copied:", result)
