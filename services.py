"""BOM lookups, deductions, adjustments, imports, and reports."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, selectinload

from config import normalize_karigar
from db import BOM, Counter, Inventory, Transaction, TransactionLine, dialect_name

IST = ZoneInfo("Asia/Kolkata")

TXN_PO = "PO"
TXN_DESIGNER = "DESIGNER"
TXN_ADJUST_PLUS = "ADJUST_PLUS"
TXN_ADJUST_MINUS = "ADJUST_MINUS"

CONSUMPTION_TYPES = (TXN_PO, TXN_DESIGNER)


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def fmt_date_slash(dt: datetime) -> str:
    return dt.strftime("%d/%m/%Y")


def fmt_date_dash(dt: datetime) -> str:
    return dt.strftime("%d-%m-%Y")


def _insert_stmt(table):
    if dialect_name() == "postgresql":
        return pg_insert(table)
    return sqlite_insert(table)


def _chunk(rows: list[dict], size: int) -> Iterable[list[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def chunk_size() -> int:
    return 2000 if dialect_name() == "postgresql" else 800


def bom_stats(session: Session) -> tuple[int, int]:
    total = session.scalar(select(func.count()).select_from(BOM)) or 0
    styles = session.scalar(select(func.count(func.distinct(BOM.style)))) or 0
    return int(styles), int(total)


def allocate_item_ids(session: Session, n: int) -> list[str]:
    if n <= 0:
        return []
    row = session.execute(
        select(Counter).where(Counter.name == "item_id").with_for_update()
    ).scalar_one()
    start = row.value
    row.value = start + n
    session.flush()
    return [f"ITM-{i:06d}" for i in range(start, start + n)]


def ensure_inventory_rows(session: Session, materials: list[str]) -> dict[str, Inventory]:
    """Return inventory rows for materials, creating any that are missing."""
    uniq = []
    seen = set()
    for m in materials:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            uniq.append(m)
    if not uniq:
        return {}
    existing = {
        r.material: r
        for r in session.execute(select(Inventory).where(Inventory.material.in_(uniq))).scalars()
    }
    missing = [m for m in uniq if m not in existing]
    ids = allocate_item_ids(session, len(missing))
    for material, item_id in zip(missing, ids):
        row = Inventory(material=material, stock_qty=0.0, unit="", item_id=item_id)
        session.add(row)
        existing[material] = row
    session.flush()
    return existing


@dataclass
class MaterialPreview:
    material: str
    required: float
    current: float
    shortfall: float


@dataclass
class LinePreview:
    po_no: str
    style: str
    qty: float
    status: str  # ok | shortfall | style_not_found | invalid
    message: str = ""
    materials: list[MaterialPreview] = field(default_factory=list)
    karigar_name: Optional[str] = None

    @property
    def is_error(self) -> bool:
        return self.status in ("style_not_found", "invalid")

    @property
    def is_applicable(self) -> bool:
        return self.status in ("ok", "shortfall")


def preview_po_lines(session: Session, rows: list[dict]) -> list[LinePreview]:
    parsed: list[tuple[int, str, str, float, Optional[str]]] = []
    results: list[Optional[LinePreview]] = [None] * len(rows)

    for i, row in enumerate(rows):
        po_no = str(row.get("po_no") or "").strip()
        if po_no.lower() == "nan":
            po_no = ""
        style = str(row.get("style") or "").strip()
        if style.lower() == "nan":
            style = ""
        karigar_name = normalize_karigar(row.get("karigar_name") if row.get("karigar_name") is not None else row.get("karigar"))
        qty_raw = row.get("qty")
        try:
            if qty_raw is None or (isinstance(qty_raw, float) and pd.isna(qty_raw)) or str(qty_raw).strip() == "":
                qty = 0.0
            else:
                qty = float(qty_raw)
        except (TypeError, ValueError):
            qty = 0.0
        if not po_no and not style and qty <= 0:
            continue
        if not po_no or not style or qty <= 0:
            results[i] = LinePreview(
                po_no=po_no,
                style=style,
                qty=qty,
                status="invalid",
                message="PO No., Style, and a Qty greater than 0 are required.",
                karigar_name=karigar_name,
            )
            continue
        parsed.append((i, po_no, style, qty, karigar_name))

    styles = list({p[2] for p in parsed})
    bom_by_style: dict[str, list[BOM]] = defaultdict(list)
    if styles:
        for bom in session.execute(select(BOM).where(BOM.style.in_(styles))).scalars():
            bom_by_style[bom.style].append(bom)

    needed_materials = {b.material for items in bom_by_style.values() for b in items}
    inv_map: dict[str, Inventory] = {}
    if needed_materials:
        inv_map = {
            r.material: r
            for r in session.execute(
                select(Inventory).where(Inventory.material.in_(list(needed_materials)))
            ).scalars()
        }

    for i, po_no, style, qty, karigar_name in parsed:
        items = bom_by_style.get(style) or []
        if not items:
            results[i] = LinePreview(
                po_no=po_no,
                style=style,
                qty=qty,
                status="style_not_found",
                message="BOM has no entry for this style.",
                karigar_name=karigar_name,
            )
            continue
        mats: list[MaterialPreview] = []
        any_short = False
        for b in items:
            required = float(b.qty_per_unit) * qty
            current = float(inv_map[b.material].stock_qty) if b.material in inv_map else 0.0
            shortfall = max(0.0, required - current)
            if shortfall > 0:
                any_short = True
            mats.append(
                MaterialPreview(
                    material=b.material,
                    required=required,
                    current=current,
                    shortfall=shortfall,
                )
            )
        results[i] = LinePreview(
            po_no=po_no,
            style=style,
            qty=qty,
            status="shortfall" if any_short else "ok",
            message="Shortfall — stock may go negative." if any_short else "OK",
            materials=mats,
            karigar_name=karigar_name,
        )

    return [r for r in results if r is not None]


def apply_po_line(session: Session, preview: LinePreview) -> Transaction:
    """All-or-nothing deduction for one PO row. Caller commits."""
    if not preview.is_applicable:
        raise ValueError(preview.message or "Line is not applicable")

    materials = [m.material for m in preview.materials]
    inv = ensure_inventory_rows(session, materials)

    txn = Transaction(
        timestamp=now_ist(),
        txn_type=TXN_PO,
        style=preview.style,
        po_no=preview.po_no,
        qty=preview.qty,
        designer_name=None,
        karigar_name=preview.karigar_name,
        note=None,
    )
    session.add(txn)
    session.flush()

    for m in preview.materials:
        row = inv[m.material]
        row.stock_qty = float(row.stock_qty) - float(m.required)
        session.add(
            TransactionLine(
                transaction_id=txn.id,
                material=m.material,
                qty_deducted=float(m.required),
            )
        )
    session.flush()
    return txn


def apply_designer_take(
    session: Session,
    designer_name: str,
    note: str,
    lines: list[tuple[str, float]],
) -> Transaction:
    designer_name = (designer_name or "").strip()
    if not designer_name:
        raise ValueError("Designer name is required.")
    cleaned: list[tuple[str, float]] = []
    for material, qty in lines:
        material = (material or "").strip()
        qty = float(qty)
        if not material or qty <= 0:
            continue
        cleaned.append((material, qty))
    if not cleaned:
        raise ValueError("Add at least one material with qty > 0.")

    names = [m for m, _ in cleaned]
    existing = {
        r.material: r
        for r in session.execute(select(Inventory).where(Inventory.material.in_(names))).scalars()
    }
    missing = [m for m in names if m not in existing]
    if missing:
        raise ValueError("Unknown materials (must exist in inventory): " + ", ".join(missing))

    txn = Transaction(
        timestamp=now_ist(),
        txn_type=TXN_DESIGNER,
        style=None,
        po_no=None,
        qty=None,
        designer_name=designer_name,
        note=(note or "").strip() or None,
    )
    session.add(txn)
    session.flush()
    for material, qty in cleaned:
        row = existing[material]
        row.stock_qty = float(row.stock_qty) - qty
        session.add(
            TransactionLine(transaction_id=txn.id, material=material, qty_deducted=qty)
        )
    session.flush()
    return txn


def apply_adjustment(
    session: Session,
    material: str,
    qty: float,
    direction: str,
    note: str = "",
    unit: Optional[str] = None,
) -> Transaction:
    material = (material or "").strip()
    qty = float(qty)
    if not material:
        raise ValueError("Material is required.")
    if qty <= 0:
        raise ValueError("Quantity must be greater than 0.")
    direction = direction.upper()
    if direction not in ("PLUS", "MINUS"):
        raise ValueError("Direction must be PLUS or MINUS.")

    inv = ensure_inventory_rows(session, [material])
    row = inv[material]
    if unit is not None and str(unit).strip():
        row.unit = str(unit).strip()

    if direction == "PLUS":
        row.stock_qty = float(row.stock_qty) + qty
        txn_type = TXN_ADJUST_PLUS
        qty_logged = qty
    else:
        row.stock_qty = float(row.stock_qty) - qty
        txn_type = TXN_ADJUST_MINUS
        qty_logged = qty

    txn = Transaction(
        timestamp=now_ist(),
        txn_type=txn_type,
        style=None,
        po_no=None,
        qty=qty,
        designer_name=None,
        note=(note or "").strip() or None,
    )
    session.add(txn)
    session.flush()
    session.add(
        TransactionLine(transaction_id=txn.id, material=material, qty_deducted=qty_logged)
    )
    session.flush()
    return txn


def set_inventory_item(
    session: Session,
    material: str,
    stock_qty: float,
    unit: str = "",
) -> tuple[Inventory, Optional[Transaction]]:
    material = (material or "").strip()
    if not material:
        raise ValueError("Material is required.")
    inv = ensure_inventory_rows(session, [material])
    row = inv[material]
    old = float(row.stock_qty)
    new = float(stock_qty)
    row.stock_qty = new
    if unit is not None:
        row.unit = str(unit).strip()
    delta = new - old
    txn = None
    if abs(delta) > 1e-12:
        txn_type = TXN_ADJUST_PLUS if delta > 0 else TXN_ADJUST_MINUS
        txn = Transaction(
            timestamp=now_ist(),
            txn_type=txn_type,
            style=None,
            po_no=None,
            qty=abs(delta),
            designer_name=None,
            note=(f"Manual stock set ({old:g} → {new:g})"),
        )
        session.add(txn)
        session.flush()
        session.add(
            TransactionLine(
                transaction_id=txn.id,
                material=material,
                qty_deducted=abs(delta),
            )
        )
    session.flush()
    return row, txn


def _as_text(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return str(val).strip()


def normalize_import_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for col in df.columns:
        key = str(col).strip().lower().replace(" ", "_")
        rename[col] = key
    return df.rename(columns=rename)


def import_bom(session: Session, df: pd.DataFrame) -> int:
    df = normalize_import_columns(df)
    required = {"style", "material", "qty_per_unit"}
    if not required.issubset(set(df.columns)):
        raise ValueError("BOM file must have columns: style, material, qty_per_unit")
    work = df[["style", "material", "qty_per_unit"]].copy()
    work["style"] = work["style"].map(_as_text)
    work["material"] = work["material"].map(_as_text)
    work["qty_per_unit"] = pd.to_numeric(work["qty_per_unit"], errors="coerce")
    work = work.dropna(subset=["style", "material", "qty_per_unit"])
    work = work[(work["style"] != "") & (work["material"] != "") & (work["style"] != "nan")]
    work = work.drop_duplicates(subset=["style", "material"], keep="last")
    rows = work.to_dict(orient="records")
    table = BOM.__table__
    size = chunk_size()
    for batch in _chunk(rows, size):
        stmt = _insert_stmt(table).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["style", "material"],
            set_={"qty_per_unit": stmt.excluded.qty_per_unit},
        )
        session.execute(stmt)
    session.flush()
    return len(rows)


def import_inventory(session: Session, df: pd.DataFrame) -> int:
    df = normalize_import_columns(df)
    if "material" not in df.columns or "stock_qty" not in df.columns:
        raise ValueError("Inventory file must have columns: material, stock_qty (unit optional)")
    cols = ["material", "stock_qty"]
    if "unit" in df.columns:
        cols.append("unit")
    work = df[cols].copy()
    work["material"] = work["material"].map(_as_text)
    work["stock_qty"] = pd.to_numeric(work["stock_qty"], errors="coerce")
    if "unit" not in work.columns:
        work["unit"] = ""
    else:
        work["unit"] = work["unit"].fillna("").astype(str).str.strip()
    work = work.dropna(subset=["material", "stock_qty"])
    work = work[(work["material"] != "") & (work["material"] != "nan")]
    work = work.drop_duplicates(subset=["material"], keep="last")
    incoming = work.to_dict(orient="records")
    names = [r["material"] for r in incoming]
    existing: dict[str, Inventory] = {}
    for batch_names in _chunk([{"m": n} for n in names], 500):
        batch_list = [x["m"] for x in batch_names]
        for r in session.execute(select(Inventory).where(Inventory.material.in_(batch_list))).scalars():
            existing[r.material] = r
    to_insert = []
    to_update = []
    for rec in incoming:
        if rec["material"] in existing:
            to_update.append(rec)
        else:
            to_insert.append(rec)
    ids = allocate_item_ids(session, len(to_insert))
    table = Inventory.__table__
    size = chunk_size()
    insert_payload = [
        {
            "material": rec["material"],
            "stock_qty": float(rec["stock_qty"]),
            "unit": rec.get("unit") or "",
            "item_id": item_id,
        }
        for rec, item_id in zip(to_insert, ids)
    ]
    for batch in _chunk(insert_payload, size):
        session.execute(table.insert(), batch)
    if to_update:
        update_payload = [
            {
                "material": rec["material"],
                "stock_qty": float(rec["stock_qty"]),
                "unit": rec.get("unit") or existing[rec["material"]].unit,
                "item_id": existing[rec["material"]].item_id,
            }
            for rec in to_update
        ]
        for batch in _chunk(update_payload, size):
            stmt = _insert_stmt(table).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["material"],
                set_={
                    "stock_qty": stmt.excluded.stock_qty,
                    "unit": stmt.excluded.unit,
                },
            )
            session.execute(stmt)
    session.flush()
    return len(incoming)


def list_inventory(session: Session, search: str = "") -> list[Inventory]:
    q: Select[tuple[Inventory]] = select(Inventory).order_by(Inventory.material)
    s = (search or "").strip()
    if s:
        q = q.where(func.lower(Inventory.material).like(f"%{s.lower()}%"))
    return list(session.execute(q).scalars())


def list_materials(session: Session) -> list[str]:
    return list(
        session.execute(select(Inventory.material).order_by(Inventory.material)).scalars()
    )


def consumption_rows(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(
        select(TransactionLine, Transaction)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(Transaction.txn_type.in_(CONSUMPTION_TYPES))
        .order_by(Transaction.timestamp.asc())
    ).all()

    by_mat: dict[str, dict[str, Any]] = {}
    for line, txn in rows:
        bucket = by_mat.setdefault(
            line.material, {"material": line.material, "total": 0.0, "parts": []}
        )
        bucket["total"] += float(line.qty_deducted)
        dt = txn.timestamp
        if txn.txn_type == TXN_PO:
            bucket["parts"].append(
                f"{txn.po_no or ''} {txn.style or ''} {fmt_date_slash(dt)}".strip()
            )
        else:
            bucket["parts"].append(
                f"Designer: {txn.designer_name or ''} {fmt_date_slash(dt)}".strip()
            )

    ranked = sorted(by_mat.values(), key=lambda x: x["total"], reverse=True)
    out = []
    for i, item in enumerate(ranked, start=1):
        contrib = ", ".join(item["parts"])
        out.append(
            {
                "rank": i,
                "material": item["material"],
                "total_consumed": item["total"],
                "contributions": contrib,
                "display": f"#{i} {item['material']} ({contrib})",
            }
        )
    return out


def history_transactions(session: Session, limit: int = 500) -> list[Transaction]:
    return list(
        session.execute(
            select(Transaction)
            .options(selectinload(Transaction.lines))
            .order_by(Transaction.timestamp.desc(), Transaction.id.desc())
            .limit(limit)
        ).scalars()
    )


def transaction_lines_flat(session: Session) -> pd.DataFrame:
    rows = session.execute(
        select(
            TransactionLine.id,
            Transaction.txn_type,
            Transaction.style,
            Transaction.po_no,
            Transaction.designer_name,
            Transaction.karigar_name,
            Transaction.timestamp,
            TransactionLine.material,
            TransactionLine.qty_deducted,
            Transaction.qty,
            Transaction.note,
        )
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .order_by(Transaction.timestamp.desc(), TransactionLine.id.desc())
    ).all()
    return pd.DataFrame(
        [
            {
                "line_id": r.id,
                "txn_type": r.txn_type,
                "style": r.style,
                "po_no": r.po_no,
                "designer_name": r.designer_name,
                "karigar_name": r.karigar_name,
                "timestamp": r.timestamp,
                "material": r.material,
                "qty_deducted": r.qty_deducted,
                "style_qty": r.qty,
                "note": r.note,
            }
            for r in rows
        ]
    )


def itemwise_consumption(session: Session) -> pd.DataFrame:
    inv = {
        r.material: r
        for r in session.execute(select(Inventory)).scalars()
    }
    rows = session.execute(
        select(TransactionLine.material, TransactionLine.qty_deducted, Transaction.timestamp)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(Transaction.txn_type.in_(CONSUMPTION_TYPES))
    ).all()
    # material -> date -> qty
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for material, qty, ts in rows:
        grouped[material][fmt_date_dash(ts)] += float(qty)

    out = []
    for material, by_date in grouped.items():
        item = inv.get(material)
        dated = []
        for d, q in by_date.items():
            try:
                parsed = datetime.strptime(d, "%d-%m-%Y")
            except ValueError:
                parsed = datetime.min
            dated.append((parsed, d, q))
        dated.sort(key=lambda x: x[0])
        parts = [f"{_fmt_qty(q)} Qty ({d})" for _, d, q in dated]
        out.append(
            {
                "Item ID": item.item_id if item else "",
                "Material Name": material,
                "Qty Used": ", ".join(parts),
            }
        )
    out.sort(key=lambda r: r["Material Name"])
    return pd.DataFrame(out)


def _fmt_qty(q: float) -> str:
    if abs(q - round(q)) < 1e-9:
        return str(int(round(q)))
    return f"{q:g}"


def bom_dataframe(session: Session) -> pd.DataFrame:
    rows = session.execute(select(BOM).order_by(BOM.style, BOM.material)).scalars()
    return pd.DataFrame(
        [{"style": r.style, "material": r.material, "qty_per_unit": r.qty_per_unit} for r in rows]
    )


def inventory_dataframe(session: Session) -> pd.DataFrame:
    rows = session.execute(select(Inventory).order_by(Inventory.item_id)).scalars()
    return pd.DataFrame(
        [
            {
                "item_id": r.item_id,
                "material": r.material,
                "stock_qty": r.stock_qty,
                "unit": r.unit,
            }
            for r in rows
        ]
    )


def movement_payload(txn: Transaction, session: Session | None = None) -> list[dict[str, Any]]:
    lines = txn.lines
    if session is not None:
        inv = {
            r.material: r
            for r in session.execute(
                select(Inventory).where(Inventory.material.in_([ln.material for ln in lines] or [""]))
            ).scalars()
        }
    else:
        inv = {}
    sign = -1 if txn.txn_type in (TXN_PO, TXN_DESIGNER, TXN_ADJUST_MINUS) else 1
    if txn.txn_type == TXN_ADJUST_PLUS:
        sign = 1
    out = []
    for ln in lines:
        item = inv.get(ln.material)
        qty_change = sign * abs(float(ln.qty_deducted))
        out.append(
            {
                "timestamp": txn.timestamp.strftime("%Y-%m-%d %H:%M:%S") if txn.timestamp else "",
                "type": txn.txn_type,
                "item_id": item.item_id if item else "",
                "material": ln.material,
                "qty_change": qty_change,
                "note": txn.note or "",
                "stock_after": float(item.stock_qty) if item else "",
                "po_no": txn.po_no or "",
                "style": txn.style or "",
                "designer_name": txn.designer_name or "",
            }
        )
    return out
