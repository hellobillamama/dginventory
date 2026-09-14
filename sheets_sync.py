"""Push live inventory and plus/minus movement history to Google Sheets."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import pandas as pd

_ROOT = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_PATH = os.path.join(_ROOT, "google-credentials.json")
SETTINGS_PATH = os.path.join(_ROOT, "sheets_settings.json")

INVENTORY_HEADERS = ["Item ID", "Material", "Stock Qty", "Unit", "Last Updated"]
HISTORY_HEADERS = [
    "Timestamp",
    "Type",
    "Item ID",
    "Material",
    "Qty Change",
    "Stock After",
    "Note",
    "PO No",
    "Style",
    "Designer",
    "Karigar",
]

# Public spreadsheet ID for DG Inventory (not a secret). Cloud still needs the JSON key.
DEFAULT_SPREADSHEET_ID = "1rG0gTby3J0HW8lGESyZOQIH4A5CH3wLvVxj6P0l8hhM"


def _secrets() -> dict:
    try:
        import streamlit as st

        return dict(st.secrets)
    except Exception:
        return {}


def parse_spreadsheet_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", value)
    if match:
        return match.group(1)
    return value.split("?")[0].strip()


def _local_settings() -> dict:
    if os.path.isfile(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def save_local_settings(spreadsheet_id: str, inventory_worksheet: str = "Inventory", history_worksheet: str = "Stock Movements") -> None:
    existing = _local_settings()
    payload = {
        "spreadsheet_id": parse_spreadsheet_id(spreadsheet_id),
        "inventory_worksheet": inventory_worksheet or "Inventory",
        "history_worksheet": history_worksheet or "Stock Movements",
        "human_editor": existing.get("human_editor") or "",
    }
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def save_service_account_json(info: dict) -> str:
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise ValueError("Upload a Google Cloud service-account JSON key (type must be service_account).")
    if not info.get("client_email") or not info.get("private_key"):
        raise ValueError("That JSON is missing client_email or private_key.")
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    return str(info["client_email"])


def service_account_email() -> str:
    info = _service_account_info()
    return str((info or {}).get("client_email") or "")


def sheets_config() -> dict[str, Any]:
    sec = _secrets()
    gs = sec.get("google_sheets") or {}
    if hasattr(gs, "to_dict"):
        gs = dict(gs)
    local = _local_settings()
    spreadsheet_id = parse_spreadsheet_id(
        os.environ.get("GOOGLE_SHEET_ID")
        or gs.get("spreadsheet_id")
        or sec.get("GOOGLE_SHEET_ID")
        or local.get("spreadsheet_id")
        or DEFAULT_SPREADSHEET_ID
        or ""
    )
    inventory_ws = gs.get("inventory_worksheet") or local.get("inventory_worksheet") or "Inventory"
    history_ws = gs.get("history_worksheet") or local.get("history_worksheet") or "Stock Movements"
    info = _service_account_info()
    return {
        "spreadsheet_id": spreadsheet_id,
        "inventory_worksheet": inventory_ws,
        "history_worksheet": history_ws,
        "configured": bool(spreadsheet_id and info),
        "has_credentials": bool(info),
        "client_email": str((info or {}).get("client_email") or ""),
    }


def _service_account_info() -> Optional[dict]:
    sec = _secrets()
    info = sec.get("gcp_service_account")
    if info:
        if hasattr(info, "to_dict"):
            return dict(info)
        if isinstance(info, dict):
            return info
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if raw:
        path = raw.strip()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        return json.loads(raw)
    if os.path.isfile(CREDENTIALS_PATH):
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return None


def _explain_sheets_error(exc: BaseException) -> str:
    messages = []
    seen = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).strip()
        if text and text not in messages:
            messages.append(text)
        cur = cur.__cause__ or cur.__context__
    blob = " ".join(messages)
    if "Google Sheets API has not been used" in blob or "sheets.googleapis.com" in blob:
        return (
            "Google Sheets API is not enabled on this Cloud project. "
            "Open https://console.developers.google.com/apis/api/sheets.googleapis.com/overview?project=853565533157 "
            "click Enable, wait 1-2 minutes, then click Save and test again. "
            "Also enable Drive API: https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=853565533157"
        )
    if "Google Drive API has not been used" in blob or "drive.googleapis.com" in blob:
        return (
            "Google Drive API is not enabled. "
            "Open https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=853565533157 "
            "click Enable, then retry."
        )
    if "403" in blob or "PERMISSION" in blob.upper() or isinstance(exc, PermissionError):
        email = service_account_email() or "the service account"
        return (
            f"No permission to open the spreadsheet as {email}. "
            f"Share the Google Sheet with {email} as Editor. "
            + (blob if blob else "")
        )
    return blob or type(exc).__name__


def _open_spreadsheet():
    cfg = sheets_config()
    if not cfg["has_credentials"]:
        raise RuntimeError("No service-account JSON saved yet.")
    if not cfg["spreadsheet_id"]:
        raise RuntimeError("No Google Sheet ID/URL saved yet.")
    try:
        gc = _client()
        return gc.open_by_key(cfg["spreadsheet_id"]), cfg
    except Exception as exc:
        raise RuntimeError(_explain_sheets_error(exc)) from exc


def test_connection() -> str:
    sh, cfg = _open_spreadsheet()
    _ws(sh, cfg["inventory_worksheet"], INVENTORY_HEADERS)
    _ws(sh, cfg["history_worksheet"], HISTORY_HEADERS)
    return f"Connected to spreadsheet '{sh.title}' as {cfg['client_email']}."


def is_configured() -> bool:
    return bool(sheets_config()["configured"])


def _client():
    import gspread
    from google.oauth2.service_account import Credentials

    info = _service_account_info()
    if not info:
        raise RuntimeError("Google service account credentials are not configured.")
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def _ws(sh, title: str, headers: list[str]):
    try:
        ws = sh.worksheet(title)
    except Exception:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(len(headers), 10))
        ws.update(range_name="A1", values=[headers])
        return ws
    existing = ws.row_values(1)
    if existing != headers:
        if not existing:
            ws.update(range_name="A1", values=[headers])
        elif existing[: len(headers)] != headers:
            # keep existing headers if the sheet was customized; only seed when empty
            pass
    return ws


def sync_inventory(inventory_df: pd.DataFrame, last_updated: str) -> None:
    sh, cfg = _open_spreadsheet()
    ws = _ws(sh, cfg["inventory_worksheet"], INVENTORY_HEADERS)

    values = [INVENTORY_HEADERS]
    if inventory_df is not None and not inventory_df.empty:
        for rec in inventory_df.to_dict(orient="records"):
            values.append(
                [
                    rec.get("item_id", ""),
                    rec.get("material", ""),
                    rec.get("stock_qty", ""),
                    rec.get("unit", ""),
                    last_updated,
                ]
            )
    ws.clear()
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")


def append_movements(rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    sh, cfg = _open_spreadsheet()
    ws = _ws(sh, cfg["history_worksheet"], HISTORY_HEADERS)
    payload = []
    for r in rows:
        payload.append(
            [
                r.get("timestamp", ""),
                r.get("type", ""),
                r.get("item_id", ""),
                r.get("material", ""),
                r.get("qty_change", ""),
                r.get("stock_after", ""),
                r.get("note", ""),
                r.get("po_no", ""),
                r.get("style", ""),
                r.get("designer_name", ""),
                r.get("karigar_name", ""),
            ]
        )
    ws.append_rows(payload, value_input_option="USER_ENTERED")


def _movement_row(r: dict[str, Any]) -> list:
    return [
        r.get("timestamp", ""),
        r.get("type", ""),
        r.get("item_id", ""),
        r.get("material", ""),
        r.get("qty_change", ""),
        r.get("stock_after", ""),
        r.get("note", ""),
        r.get("po_no", ""),
        r.get("style", ""),
        r.get("designer_name", ""),
        r.get("karigar_name", ""),
    ]


def rebuild_stock_movements(rows: list[dict[str, Any]]) -> int:
    sh, cfg = _open_spreadsheet()
    ws = _ws(sh, cfg["history_worksheet"], HISTORY_HEADERS)
    ws.clear()
    ws.update(range_name="A1", values=[HISTORY_HEADERS], value_input_option="USER_ENTERED")
    payload = [_movement_row(r) for r in rows]
    for i in range(0, len(payload), 2000):
        ws.append_rows(payload[i : i + 2000], value_input_option="USER_ENTERED")
    return len(payload)


def sync_after_change(inventory_df: pd.DataFrame, movement_rows: list[dict[str, Any]], last_updated: str) -> str:
    """Append Stock Movements, then refresh the Inventory tab."""
    if not is_configured():
        return "Google Sheets is not configured — skipped."
    notes = []
    try:
        append_movements(movement_rows)
        if movement_rows:
            notes.append("Stock Movements")
    except Exception as exc:
        raise RuntimeError("Stock Movements sync failed: " + _explain_sheets_error(exc)) from exc
    try:
        sync_inventory(inventory_df, last_updated)
        notes.append("Inventory")
    except Exception as exc:
        notes.append("Inventory snapshot failed: " + _explain_sheets_error(exc))
    return "Google Sheets updated (" + ", ".join(notes) + ")."
