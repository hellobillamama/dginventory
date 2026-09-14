"""Material Inventory Deduction System — Streamlit UI."""

from __future__ import annotations

import io
import json
from contextlib import contextmanager

import pandas as pd
import streamlit as st

from config import KARIGAR_NONE, karigar_select_options, normalize_karigar
from db import SessionLocal, dialect_name, get_engine, running_on_streamlit_cloud
from services import (
    apply_adjustment,
    apply_designer_take,
    apply_po_line,
    bom_dataframe,
    bom_stats,
    consumption_rows,
    history_transactions,
    import_bom,
    import_inventory,
    inventory_dataframe,
    itemwise_consumption,
    list_inventory,
    list_materials,
    movement_payload,
    now_ist,
    preview_po_lines,
    set_inventory_item,
    transaction_lines_flat,
    _as_text,
)
import importlib
import sheets_sync

importlib.reload(sheets_sync)
from sheets_sync import (
    save_local_settings,
    save_service_account_json,
    sheets_config,
    sync_after_change,
    test_connection,
)

st.set_page_config(page_title="DG Inventory", page_icon="📦", layout="wide")


@contextmanager
def session_scope(commit: bool = False):
    session = SessionLocal()
    try:
        yield session
        if commit:
            session.commit()
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _push_sheets(txns: list) -> str:
    try:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        from db import Transaction as Txn

        with session_scope(False) as session:
            moves = []
            ids = [t.id for t in txns if getattr(t, "id", None)]
            if ids:
                loaded = session.execute(
                    select(Txn).options(selectinload(Txn.lines)).where(Txn.id.in_(ids))
                ).scalars()
                for t in loaded:
                    moves.extend(movement_payload(t, session))
            inv = inventory_dataframe(session)
            stamp = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        return sync_after_change(inv, moves, stamp)
    except Exception as exc:
        return f"Inventory saved locally, but Google Sheets sync failed: {exc}"


def _manual_full_sync() -> str:
    try:
        with session_scope(False) as session:
            inv = inventory_dataframe(session)
            stamp = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        return sync_after_change(inv, [], stamp)
    except Exception as exc:
        return f"Google Sheets sync failed: {exc}"


def _norm_col(name: str) -> str:
    return (
        str(name)
        .strip()
        .lower()
        .replace(" ", "_")
        .replace(".", "")
        .replace("-", "_")
        .replace("#", "")
    )


_COL_ALIASES = {
    "po_no": ["po_no", "po", "po_number", "pono", "po_no_", "purchase_order"],
    "style": ["style", "style_no", "style_code", "style_name"],
    "qty": ["qty", "quantity", "qty_pcs", "pcs", "qnty"],
    "material": ["material", "material_name", "item", "item_name", "item_description"],
    "direction": ["direction", "type", "plus_minus", "add_minus", "action"],
    "note": ["note", "notes", "remarks", "remark"],
    "designer_name": ["designer_name", "designer", "name"],
    "unit": ["unit", "uom"],
    "stock_qty": ["stock_qty", "stock", "qty", "quantity"],
    "karigar": ["karigar", "karigar_name", "karigarname", "artisan"],
}


def _read_uploaded_table(uploaded) -> pd.DataFrame:
    name = (uploaded.name or "").lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded)
    return pd.read_excel(uploaded)


def _map_table(df: pd.DataFrame, wanted: list[str]) -> pd.DataFrame:
    lookup = {_norm_col(c): c for c in df.columns}
    out = pd.DataFrame()
    for canon in wanted:
        src = None
        for alias in _COL_ALIASES.get(canon, []) + [canon]:
            if alias in lookup:
                src = lookup[alias]
                break
        if src is not None:
            out[canon] = df[src]
        else:
            out[canon] = None
    return out


def _template_bytes(columns: list[str], sample: dict | None = None) -> bytes:
    row = sample or {c: "" for c in columns}
    buf = io.BytesIO()
    pd.DataFrame([row], columns=columns).to_excel(buf, index=False, engine="openpyxl")
    buf.seek(0)
    return buf.getvalue()


def _excel_load_controls(label: str, key: str, columns: list[str], sample: dict | None = None):
    st.markdown(f"**Excel / CSV upload** — columns: {', '.join(columns)}")
    left, right = st.columns([3, 1])
    with left:
        uploaded = st.file_uploader(label, type=["xlsx", "xls", "csv"], key=key)
    with right:
        st.download_button(
            "Download template",
            data=_template_bytes(columns, sample),
            file_name=f"{key}_template.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key}_template",
        )
    return uploaded


get_engine()

st.title("DG Inventory")
st.caption("BOM-based PO deduction, designer take, stock adjustments, and consumption reporting.")

with st.sidebar:
    st.header("Status")
    with session_scope(False) as session:
        n_styles, n_bom = bom_stats(session)
    st.metric("Distinct styles", f"{n_styles:,}")
    st.metric("BOM rows", f"{n_bom:,}")
    st.write(f"Database: `{dialect_name()}`")
    if dialect_name() == "sqlite" and running_on_streamlit_cloud():
        st.error(
            "This website is using a temporary empty database. "
            "Your real data is on the office PC. Set DATABASE_URL (Neon/Supabase) "
            "in Manage app → Secrets, then copy the PC database once (Setup / Import)."
        )
    elif dialect_name() == "sqlite":
        st.info("This PC stores data in dg_inventory.db. For phones/cloud, migrate to Postgres in Setup / Import.")
    else:
        st.success("Persistent Postgres — saved data stays after refresh and sleep.")
    cfg = sheets_config()
    if cfg["configured"]:
        st.success("Google Sheets connected")
        st.caption(f"Workbook `{cfg['spreadsheet_id'][:8]}…`")
    else:
        st.warning("Google Sheets not configured")
        st.caption("Open **Setup / Import** → Google Sheets and paste your sheet URL + service-account JSON.")
    if st.button("Sync inventory to Sheets now"):
        with st.spinner("Syncing…"):
            msg = _manual_full_sync()
        if "fail" in msg.lower() or "not configured" in msg.lower() or "not enabled" in msg.lower():
            st.error(msg)
        else:
            st.success(msg)

tabs = st.tabs(
    [
        "PO Deduction",
        "Designer Take",
        "Inventory",
        "Adjustments",
        "Consumption",
        "History",
        "Export",
        "Setup / Import",
    ]
)


# ---------------------------------------------------------------------------
# PO Deduction
# ---------------------------------------------------------------------------
with tabs[0]:
    st.subheader("PO Deduction")
    st.write(
        "Enter rows, paste from Excel, or upload a file. Columns: **PO No.**, **Style**, **Qty**, optional **Karigar**. "
        "Preview every line, then confirm. Shortfalls warn but still deduct (stock can go negative). "
        "Unknown styles are skipped. Karigar is optional."
    )
    if "po_grid" not in st.session_state:
        st.session_state.po_grid = pd.DataFrame(
            {"po_no": [""], "style": [""], "qty": [None], "karigar": [KARIGAR_NONE]}
        )
    if "karigar" not in st.session_state.po_grid.columns:
        st.session_state.po_grid["karigar"] = KARIGAR_NONE
    if "po_editor_nonce" not in st.session_state:
        st.session_state.po_editor_nonce = 0

    po_upload = _excel_load_controls(
        "Upload PO list (.xlsx or .csv)",
        "po_xlsx",
        ["po_no", "style", "qty", "karigar"],
        {"po_no": "DGPOIN00853", "style": "ERL5867GLD", "qty": 10, "karigar": KARIGAR_NONE},
    )
    if po_upload is not None:
        try:
            raw = _read_uploaded_table(po_upload)
            mapped = _map_table(raw, ["po_no", "style", "qty", "karigar"])
            mapped["po_no"] = mapped["po_no"].map(_as_text)
            mapped["style"] = mapped["style"].map(_as_text)
            mapped["qty"] = pd.to_numeric(mapped["qty"], errors="coerce")
            mapped["karigar"] = mapped["karigar"].map(
                lambda v: normalize_karigar(v) or KARIGAR_NONE
            )
            mapped = mapped[(mapped["po_no"] != "") | (mapped["style"] != "") | mapped["qty"].notna()]
            st.caption(f"{len(mapped):,} row(s) in file")
            st.dataframe(mapped.head(20), use_container_width=True, hide_index=True)
            if st.button("Load file into PO table", type="secondary"):
                st.session_state.po_grid = mapped.reset_index(drop=True)
                st.session_state.po_editor_nonce += 1
                st.session_state.po_previews = None
                st.rerun()
        except Exception as exc:
            st.error(f"Could not read that file: {exc}")

    po_edited = st.data_editor(
        st.session_state.po_grid,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "po_no": st.column_config.TextColumn("PO No.", required=False, width="medium"),
            "style": st.column_config.TextColumn("Style", required=False, width="medium"),
            "qty": st.column_config.NumberColumn("Qty", min_value=0.0, step=1.0, format="%g"),
            "karigar": st.column_config.SelectboxColumn(
                "Karigar Name",
                options=karigar_select_options(),
                default=KARIGAR_NONE,
                required=False,
            ),
        },
        key=f"po_editor_{st.session_state.po_editor_nonce}",
    )
    st.session_state.po_grid = po_edited

    c1, c2 = st.columns(2)
    preview_clicked = c1.button("Preview all lines", type="primary")
    confirm_clicked = c2.button("Confirm all valid lines")

    def _po_row_dicts(df: pd.DataFrame) -> list[dict]:
        rows = []
        for rec in df.to_dict(orient="records"):
            rows.append(
                {
                    "po_no": rec.get("po_no"),
                    "style": rec.get("style"),
                    "qty": rec.get("qty"),
                    "karigar_name": rec.get("karigar"),
                }
            )
        return rows

    if preview_clicked:
        with session_scope(False) as session:
            st.session_state.po_previews = preview_po_lines(session, _po_row_dicts(po_edited))

    previews = st.session_state.get("po_previews")
    if previews:
        ok_n = sum(1 for p in previews if p.status == "ok")
        short_n = sum(1 for p in previews if p.status == "shortfall")
        err_n = sum(1 for p in previews if p.is_error)
        st.write(f"**Preview:** {ok_n} OK · {short_n} shortfall · {err_n} errors")
        for i, p in enumerate(previews, start=1):
            label = f"{i}. {p.po_no} / {p.style} / qty {p.qty:g} — {p.status.upper()}"
            if p.karigar_name:
                label += f" · Karigar: {p.karigar_name}"
            if p.status == "ok":
                st.success(label)
            elif p.status == "shortfall":
                st.warning(label)
                with st.expander("Short materials"):
                    short_df = pd.DataFrame(
                        [
                            {
                                "material": m.material,
                                "required": m.required,
                                "current": m.current,
                                "shortfall": m.shortfall,
                            }
                            for m in p.materials
                            if m.shortfall > 0
                        ]
                    )
                    st.dataframe(short_df, use_container_width=True, hide_index=True)
            else:
                st.error(f"{label} — {p.message}")

    if confirm_clicked:
        with session_scope(False) as session:
            fresh = preview_po_lines(session, _po_row_dicts(po_edited))
        succeeded, failed, txns = [], [], []
        for p in fresh:
            if p.is_error:
                failed.append((p, p.message))
                continue
            session = SessionLocal()
            try:
                txn = apply_po_line(session, p)
                session.commit()
                succeeded.append(p)
                txns.append(txn)
            except Exception as exc:
                session.rollback()
                failed.append((p, str(exc)))
            finally:
                session.close()
        st.success(f"Applied {len(succeeded)} PO line(s).")
        if failed:
            st.error("Skipped / failed:")
            for p, reason in failed:
                st.write(f"- {p.po_no} {p.style} qty {p.qty}: {reason}")
        if txns:
            msg = _push_sheets(txns)
            if msg:
                st.info(msg)
        st.session_state.po_previews = None


# ---------------------------------------------------------------------------
# Designer Take
# ---------------------------------------------------------------------------
with tabs[1]:
    st.subheader("Designer Take")
    if "pending_designer_name" in st.session_state:
        st.session_state.designer_name = st.session_state.pop("pending_designer_name")
    if "pending_designer_note" in st.session_state:
        st.session_state.designer_note = st.session_state.pop("pending_designer_note")
    with session_scope(False) as session:
        materials = list_materials(session)
    designer = st.text_input("Designer name", key="designer_name")
    note = st.text_input("Note (optional)", key="designer_note")
    if "designer_grid" not in st.session_state:
        st.session_state.designer_grid = pd.DataFrame(
            {"material": pd.Series(dtype="string"), "qty": pd.Series(dtype="float")}
        )
    if "designer_editor_nonce" not in st.session_state:
        st.session_state.designer_editor_nonce = 0

    d_upload = _excel_load_controls(
        "Upload designer take list (.xlsx or .csv)",
        "designer_xlsx",
        ["material", "qty"],
        {"material": "DROP RED NATURAL STONE", "qty": 2},
    )
    if d_upload is not None:
        try:
            raw = _read_uploaded_table(d_upload)
            mapped = _map_table(raw, ["material", "qty", "designer_name", "note"])
            mapped["material"] = mapped["material"].map(_as_text)
            mapped["qty"] = pd.to_numeric(mapped["qty"], errors="coerce")
            shown = mapped[(mapped["material"] != "") & mapped["qty"].notna()][["material", "qty"]]
            names = [n for n in mapped["designer_name"].map(_as_text).tolist() if n]
            notes = [n for n in mapped["note"].map(_as_text).tolist() if n]
            st.caption(f"{len(shown):,} row(s) in file")
            st.dataframe(shown.head(20), use_container_width=True, hide_index=True)
            if st.button("Load file into designer table", type="secondary"):
                st.session_state.designer_grid = shown.reset_index(drop=True)
                st.session_state.designer_editor_nonce += 1
                if names:
                    st.session_state.pending_designer_name = names[0]
                if notes:
                    st.session_state.pending_designer_note = notes[0]
                st.rerun()
        except Exception as exc:
            st.error(f"Could not read that file: {exc}")

    extra_mats = [
        str(x)
        for x in st.session_state.designer_grid.get("material", pd.Series(dtype="string")).dropna().tolist()
        if str(x).strip()
    ]
    mat_options = sorted(set(materials) | set(extra_mats))
    d_edited = st.data_editor(
        st.session_state.designer_grid,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "material": st.column_config.SelectboxColumn("Material", options=mat_options or [""]),
            "qty": st.column_config.NumberColumn("Qty", min_value=0.0, step=1.0, format="%g"),
        },
        key=f"designer_editor_{st.session_state.designer_editor_nonce}",
    )
    st.session_state.designer_grid = d_edited
    if st.button("Confirm designer take", type="primary"):
        lines = []
        for rec in d_edited.to_dict(orient="records"):
            mat = rec.get("material")
            qty = rec.get("qty")
            if mat and qty:
                lines.append((str(mat), float(qty)))
        session = SessionLocal()
        try:
            txn = apply_designer_take(session, designer, note, lines)
            session.commit()
            st.success("Designer take recorded.")
            msg = _push_sheets([txn])
            if msg:
                st.info(msg)
        except Exception as exc:
            session.rollback()
            st.error(str(exc))
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
with tabs[2]:
    st.subheader("Live inventory")
    search = st.text_input("Search material name", key="inv_search")
    with session_scope(False) as session:
        items = list_inventory(session, search)
        df = pd.DataFrame(
            [
                {
                    "item_id": r.item_id,
                    "material": r.material,
                    "stock_qty": r.stock_qty,
                    "unit": r.unit,
                }
                for r in items
            ]
        )
    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"{len(df):,} item(s)")

    st.markdown("#### Upload inventory Excel")
    inv_tab_file = _excel_load_controls(
        "Upload inventory (.xlsx or .csv)",
        "inv_tab_xlsx",
        ["material", "stock_qty", "unit"],
        {"material": "DROP RED NATURAL STONE", "stock_qty": 100, "unit": "PCS"},
    )
    if inv_tab_file is not None:
        try:
            inv_up = _read_uploaded_table(inv_tab_file)
            st.dataframe(inv_up.head(20), use_container_width=True, hide_index=True)
            st.caption(f"{len(inv_up):,} row(s) in file")
            if st.button("Import inventory from file", type="primary"):
                session = SessionLocal()
                try:
                    n = import_inventory(session, inv_up)
                    session.commit()
                    st.success(f"Imported / updated {n:,} inventory row(s).")
                    st.info(_manual_full_sync())
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    st.error(str(exc))
                finally:
                    session.close()
        except Exception as exc:
            st.error(f"Could not read that file: {exc}")


# ---------------------------------------------------------------------------
# Adjustments
# ---------------------------------------------------------------------------
with tabs[3]:
    st.subheader("Add / minus inventory")
    st.write("Plus increases stock. Minus decreases stock. Each confirm is logged and synced to Google Sheets.")
    with session_scope(False) as session:
        materials = list_materials(session)
    col_a, col_b = st.columns(2)
    with col_a:
        material = st.selectbox("Material", options=materials, key="adj_material") if materials else None
        new_material = st.text_input("Or type a new material name", key="adj_new_material")
        direction = st.radio("Direction", ["Plus", "Minus"], horizontal=True, key="adj_dir")
        qty = st.number_input("Quantity", min_value=0.0, step=1.0, key="adj_qty")
        unit = st.text_input("Unit (optional, for new / update)", key="adj_unit")
        adj_note = st.text_input("Note (optional)", key="adj_note")
        if st.button("Apply adjustment", type="primary"):
            target = (new_material or "").strip() or (material or "")
            session = SessionLocal()
            try:
                txn = apply_adjustment(
                    session,
                    material=target,
                    qty=float(qty),
                    direction="PLUS" if direction == "Plus" else "MINUS",
                    note=adj_note,
                    unit=unit,
                )
                session.commit()
                st.success(f"{direction} {qty:g} on {target}")
                msg = _push_sheets([txn])
                if msg:
                    st.info(msg)
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
            finally:
                session.close()

    with col_b:
        st.markdown("**Recent plus / minus history**")
        with session_scope(False) as session:
            hist = [
                t
                for t in history_transactions(session, limit=200)
                if t.txn_type in ("ADJUST_PLUS", "ADJUST_MINUS")
            ]
            for t in hist[:50]:
                sign = "+" if t.txn_type == "ADJUST_PLUS" else "−"
                mats = ", ".join(f"{ln.material} {sign}{ln.qty_deducted:g}" for ln in t.lines)
                st.write(f"{t.timestamp.strftime('%d/%m/%Y %H:%M')} · {mats} · {t.note or ''}")

    st.markdown("#### Upload plus / minus Excel")
    adj_upload = _excel_load_controls(
        "Upload adjustments (.xlsx or .csv)",
        "adj_xlsx",
        ["material", "qty", "direction", "note"],
        {"material": "DROP RED NATURAL STONE", "qty": 5, "direction": "PLUS", "note": ""},
    )
    if adj_upload is not None:
        try:
            raw = _read_uploaded_table(adj_upload)
            mapped = _map_table(raw, ["material", "qty", "direction", "note", "unit"])
            mapped["material"] = mapped["material"].map(_as_text)
            mapped["qty"] = pd.to_numeric(mapped["qty"], errors="coerce")
            mapped["direction"] = mapped["direction"].map(_as_text).str.upper()
            mapped["note"] = mapped["note"].map(_as_text)
            mapped["unit"] = mapped["unit"].map(_as_text)
            mapped = mapped[(mapped["material"] != "") & mapped["qty"].notna() & (mapped["qty"] > 0)]
            st.dataframe(mapped.head(30), use_container_width=True, hide_index=True)
            st.caption("direction: PLUS or MINUS (also accepts + / - / add / deduct)")
            if st.button("Apply all adjustments from file", type="primary"):
                txns = []
                errors = []
                for rec in mapped.to_dict(orient="records"):
                    direction = rec.get("direction") or "PLUS"
                    if direction in ("-", "MINUS", "DEDUCT", "LESS", "REMOVE"):
                        direction = "MINUS"
                    else:
                        direction = "PLUS"
                    session = SessionLocal()
                    try:
                        txn = apply_adjustment(
                            session,
                            material=rec["material"],
                            qty=float(rec["qty"]),
                            direction=direction,
                            note=rec.get("note") or "Excel upload",
                            unit=rec.get("unit") or "",
                        )
                        session.commit()
                        txns.append(txn)
                    except Exception as exc:
                        session.rollback()
                        errors.append(f"{rec['material']}: {exc}")
                    finally:
                        session.close()
                st.success(f"Applied {len(txns)} adjustment(s).")
                if errors:
                    st.error("\n".join(errors[:20]))
                if txns:
                    st.info(_push_sheets(txns))
        except Exception as exc:
            st.error(f"Could not read that file: {exc}")


# ---------------------------------------------------------------------------
# Consumption
# ---------------------------------------------------------------------------
with tabs[4]:
    st.subheader("Consumption")
    q = st.text_input("Search", key="cons_search")
    cons_upload = _excel_load_controls(
        "Filter by materials in Excel (.xlsx or .csv)",
        "cons_xlsx",
        ["material"],
        {"material": "DROP RED NATURAL STONE"},
    )
    file_mats = set()
    if cons_upload is not None:
        try:
            raw = _read_uploaded_table(cons_upload)
            mapped = _map_table(raw, ["material"])
            file_mats = {m.lower() for m in mapped["material"].map(_as_text).tolist() if m}
            st.caption(f"Filtering to {len(file_mats):,} material name(s) from the file")
        except Exception as exc:
            st.error(f"Could not read that file: {exc}")
    with session_scope(False) as session:
        rows = consumption_rows(session)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["display"].lower() or ql in r["material"].lower()]
    if file_mats:
        rows = [r for r in rows if r["material"].lower() in file_mats]
    if not rows:
        st.info("No consumption yet.")
    else:
        st.caption(f"{len(rows):,} material(s) — sorted by total consumed")
        for r in rows[:400]:
            st.markdown(f"**{r['display']}**")
            st.caption(f"Total consumed: {r['total_consumed']:g}")
        if len(rows) > 400:
            st.info("Showing the top 400. Use search to narrow, or Export for the full list.")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
with tabs[5]:
    st.subheader("Transaction history")
    hist_upload = _excel_load_controls(
        "Filter history by PO / style / material Excel (.xlsx or .csv)",
        "hist_xlsx",
        ["po_no", "style", "material"],
        {"po_no": "DGPOIN00853", "style": "", "material": ""},
    )
    filter_pos, filter_styles, filter_mats = set(), set(), set()
    if hist_upload is not None:
        try:
            raw = _read_uploaded_table(hist_upload)
            mapped = _map_table(raw, ["po_no", "style", "material"])
            filter_pos = {x.lower() for x in mapped["po_no"].map(_as_text).tolist() if x}
            filter_styles = {x.lower() for x in mapped["style"].map(_as_text).tolist() if x}
            filter_mats = {x.lower() for x in mapped["material"].map(_as_text).tolist() if x}
            st.caption(
                f"Filters — PO: {len(filter_pos)}, style: {len(filter_styles)}, material: {len(filter_mats)}"
            )
        except Exception as exc:
            st.error(f"Could not read that file: {exc}")
    with session_scope(False) as session:
        txns = history_transactions(session, limit=500)
        if filter_pos or filter_styles or filter_mats:
            kept = []
            for t in txns:
                po = (t.po_no or "").lower()
                style = (t.style or "").lower()
                mats = {(ln.material or "").lower() for ln in t.lines}
                if filter_pos and po in filter_pos:
                    kept.append(t)
                elif filter_styles and style in filter_styles:
                    kept.append(t)
                elif filter_mats and mats & filter_mats:
                    kept.append(t)
            txns = kept
        if not txns:
            st.info("No transactions yet.")
        for t in txns:
            parts = [
                f"#{t.id}",
                t.timestamp.strftime("%Y-%m-%d %H:%M"),
            ]
            if t.txn_type == "PO":
                parts.append(f"PO {t.po_no}" if t.po_no else "PO")
                if t.style:
                    parts.append(f"Style {t.style}")
                if t.qty is not None:
                    parts.append(f"Qty {t.qty:g}")
                if t.karigar_name:
                    parts.append(f"Karigar: {t.karigar_name}")
            else:
                parts.append(t.txn_type)
                if t.po_no:
                    parts.append(f"PO {t.po_no}")
                if t.style:
                    parts.append(t.style)
                if t.qty is not None:
                    parts.append(f"qty {t.qty:g}")
                if t.designer_name:
                    parts.append(t.designer_name)
            title = " | ".join(parts)
            with st.expander(title):
                if t.note:
                    st.write(t.note)
                lines = pd.DataFrame(
                    [{"material": ln.material, "qty_deducted": ln.qty_deducted} for ln in t.lines]
                )
                st.dataframe(lines, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
with tabs[6]:
    st.subheader("Export Excel")
    st.write("One workbook with BOM, Inventory, Transactions, Consumption Summary, and Item-wise Consumption.")
    if st.button("Build Excel file", type="primary"):
        with session_scope(False) as session:
            bom_df = bom_dataframe(session)
            inv_df = inventory_dataframe(session)
            txn_df = transaction_lines_flat(session)
            cons = consumption_rows(session)
            cons_df = pd.DataFrame(
                [
                    {
                        "material": r["material"],
                        "total_consumed": r["total_consumed"],
                        "contributions": r["contributions"],
                    }
                    for r in cons
                ]
            )
            item_df = itemwise_consumption(session)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            bom_df.to_excel(writer, index=False, sheet_name="BOM")
            inv_df.to_excel(writer, index=False, sheet_name="Inventory")
            txn_df.to_excel(writer, index=False, sheet_name="Transactions")
            cons_df.to_excel(writer, index=False, sheet_name="Consumption Summary")
            item_df.to_excel(writer, index=False, sheet_name="Item-wise Consumption")
        buf.seek(0)
        st.download_button(
            "Download inventory_export.xlsx",
            data=buf,
            file_name=f"inventory_export_{now_ist().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ---------------------------------------------------------------------------
# Setup / Import
# ---------------------------------------------------------------------------
with tabs[7]:
    st.subheader("Setup / Import")
    with session_scope(False) as session:
        n_styles, n_bom = bom_stats(session)
    s1, s2 = st.columns(2)
    s1.metric("Distinct styles", f"{n_styles:,}")
    s2.metric("Total BOM rows", f"{n_bom:,}")

    st.markdown("### Persistent database (keep data forever)")
    st.write(
        "Streamlit Cloud **sleeps** and its SQLite file is thrown away. "
        "A Neon or Supabase Postgres URL keeps every BOM, stock, PO, and adjustment. "
        "The website and this PC then share the **same** database."
    )
    st.markdown(
        """
1. Create a free database at [Neon](https://console.neon.tech) (recommended) or [Supabase](https://supabase.com).
2. Copy the connection string (`postgresql://...`).
3. On Streamlit Cloud: **Manage app → Settings → Secrets** and paste:
   `DATABASE_URL = "postgresql://..."`
4. On this PC, paste the same URL below and copy the local `dg_inventory.db` into it **once**.
"""
    )
    pg_url = st.text_input("Postgres DATABASE_URL", type="password", key="persist_db_url")
    confirm_wipe = st.checkbox(
        "Overwrite the Postgres database with this PC's SQLite file (one-time copy)",
        key="persist_db_confirm",
    )
    if st.button("Copy office data into Postgres"):
        if not pg_url.strip():
            st.error("Paste the Postgres URL first.")
        elif running_on_streamlit_cloud():
            st.error("Run this copy from the office PC (where dg_inventory.db lives), not from the website.")
        elif not confirm_wipe:
            st.error("Tick the confirmation box. This replaces whatever is currently in Postgres.")
        else:
            try:
                from migrate_db import migrate_sqlite_to_postgres, save_database_url_secret

                save_database_url_secret(pg_url.strip())
                with st.spinner("Copying BOM, inventory, and history… this can take a minute"):
                    copied = migrate_sqlite_to_postgres(pg_url.strip())
                st.success(
                    "Copied: "
                    + ", ".join(f"{k}={v:,}" for k, v in copied.items())
                    + ". Restart this app, and add the same DATABASE_URL to Streamlit Cloud secrets, then reboot the website."
                )
            except Exception as exc:
                st.error(str(exc))

    st.divider()
    st.markdown("### Google Sheets")
    st.write(
        "Live inventory and plus/minus history sync here after every stock change. "
        "This is not optional if you want the Google Sheet to update — connect it once below."
    )
    st.markdown(
        """
1. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable **Google Sheets API** and **Google Drive API**.
2. **IAM & Admin → Service accounts → Create** → add a **JSON key** and download it.
3. Create (or open) a Google Sheet. Share it with the service account **client_email** as **Editor**.
4. Paste the sheet link and upload that JSON key, then click **Save and test**.
"""
    )
    gs_cfg = sheets_config()
    if gs_cfg["configured"]:
        st.success(f"Connected. Service account: `{gs_cfg['client_email']}`")
    elif gs_cfg["has_credentials"]:
        st.warning(f"JSON saved (`{gs_cfg['client_email']}`), but the Sheet URL/ID is still missing.")
    else:
        st.info("No credentials saved yet — that is why sync says “not configured — skipped”.")

    sheet_url = st.text_input(
        "Google Sheet URL or ID",
        value=gs_cfg.get("spreadsheet_id") or "",
        key="gs_sheet_url",
    )
    cred_file = st.file_uploader("Service account JSON key", type=["json"], key="gs_json")
    save_gs = st.button("Save and test Google Sheets", type="primary")
    if save_gs:
        try:
            if cred_file is not None:
                info = json.loads(cred_file.getvalue().decode("utf-8"))
                email = save_service_account_json(info)
                st.caption(f"Saved key for `{email}`")
            if not (sheet_url or "").strip() and not gs_cfg.get("spreadsheet_id"):
                st.error("Paste the Google Sheet URL (or ID) first.")
            else:
                save_local_settings(sheet_url or gs_cfg.get("spreadsheet_id") or "")
                msg = test_connection()
                st.success(msg)
                with st.spinner("Writing current inventory to the sheet…"):
                    st.info(_manual_full_sync())
                st.rerun()
        except Exception as exc:
            err = str(exc)
            st.error(err)
            if "404" in err or "not found" in err.lower() or "Requested entity was not found" in err:
                email = sheets_config().get("client_email") or "the service account email"
                st.warning(
                    f"Share the Google Sheet with **{email}** as Editor, then try Save and test again."
                )

    st.divider()
    st.markdown("### Upload BOM")
    st.caption("Columns: style, material, qty_per_unit (names matched case/space-insensitively). Upserts on style+material.")
    bom_file = st.file_uploader("BOM .xlsx or .csv", type=["xlsx", "csv"], key="bom_file")
    if bom_file is not None:
        if bom_file.name.lower().endswith(".csv"):
            bom_df = pd.read_csv(bom_file)
        else:
            bom_df = pd.read_excel(bom_file)
        st.dataframe(bom_df.head(50), use_container_width=True, hide_index=True)
        st.caption(f"{len(bom_df):,} row(s) in file (showing first 50)")
        if st.button("Import BOM", type="primary"):
            session = SessionLocal()
            try:
                n = import_bom(session, bom_df)
                session.commit()
                st.success(f"Imported / updated {n:,} BOM row(s).")
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
            finally:
                session.close()

    st.markdown("### Upload Inventory")
    st.caption("Columns: material, stock_qty, optional unit. Upserts on material. New items get the next ITM-###### code.")
    inv_file = st.file_uploader("Inventory .xlsx or .csv", type=["xlsx", "csv"], key="inv_file")
    if inv_file is not None:
        if inv_file.name.lower().endswith(".csv"):
            inv_up = pd.read_csv(inv_file)
        else:
            inv_up = pd.read_excel(inv_file)
        st.dataframe(inv_up.head(50), use_container_width=True, hide_index=True)
        st.caption(f"{len(inv_up):,} row(s) in file (showing first 50)")
        if st.button("Import Inventory", type="primary"):
            session = SessionLocal()
            try:
                n = import_inventory(session, inv_up)
                session.commit()
                st.success(f"Imported / updated {n:,} inventory row(s).")
                msg = _manual_full_sync()
                st.info(msg)
                st.rerun()
            except Exception as exc:
                session.rollback()
                st.error(str(exc))
            finally:
                session.close()

    st.markdown("### Manual inventory item")
    m1, m2, m3 = st.columns(3)
    man_mat = m1.text_input("Material")
    man_qty = m2.number_input("Stock qty", step=1.0, format="%g")
    man_unit = m3.text_input("Unit")
    if st.button("Save item"):
        session = SessionLocal()
        try:
            _row, txn = set_inventory_item(session, man_mat, float(man_qty), man_unit)
            session.commit()
            st.success("Saved.")
            msg = _push_sheets([txn] if txn else [])
            if msg:
                st.info(msg)
        except Exception as exc:
            session.rollback()
            st.error(str(exc))
        finally:
            session.close()
