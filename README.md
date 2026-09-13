# DG Inventory

Material inventory deduction for manufacturing: BOM lookup by style, PO and designer deductions, stock plus/minus, consumption reports, Excel export, and optional live Google Sheets sync.

## Run locally

```bash
cd DG_Inventory
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

On first run a SQLite file `dg_inventory.db` is created in this folder. No other services are required.

## Host on a free public URL (phone / anywhere)

The app is Streamlit, so the free host is **[Streamlit Community Cloud](https://share.streamlit.io/)**. Vercel is not a fit (it does not run Streamlit).

SQLite on Streamlit Cloud is wiped when the app sleeps or redeploys. For a real shared deployment, create a free Postgres database ([Neon](https://neon.tech) or [Supabase](https://supabase.com)) and paste the connection string as `DATABASE_URL`.

1. Put this project on GitHub (new repo → upload the folder, **do not** commit `dg_inventory.db`, `google-credentials.json`, or `.streamlit/secrets.toml`).
2. Sign in at [share.streamlit.io](https://share.streamlit.io/) with the same GitHub account.
3. **New app** → pick this repo → main file `app.py`.
4. In **Advanced settings → Secrets**, add at least:

```toml
DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DB?sslmode=require"
```

Google Sheets secrets can go in the same box (`[google_sheets]` and `[gcp_service_account]` from `.streamlit/secrets.toml.example`).
5. Deploy. Staff open the `*.streamlit.app` URL from any phone.

Until that is live, people on the same office Wi‑Fi can use the PC that runs:

```bash
streamlit run app.py --server.address 0.0.0.0
```

Karigar names are edited in `config.py` (`KARIGAR_NAMES`).

## Postgres (cloud)

Set `DATABASE_URL` as an environment variable or a Streamlit secret. No code changes. `postgres://` URLs are rewritten to SQLAlchemy's `postgresql+psycopg2://` automatically.

On Streamlit Community Cloud, add `DATABASE_URL` under **App settings → Secrets**.

## Google Sheets (live inventory + plus/minus history)

After every PO confirm, designer take, stock adjustment, inventory import, or manual item save, the app:

1. Rewrites the **Inventory** worksheet (item id, material, stock qty, unit, last updated).
2. Appends rows to **Stock Movements** (timestamp, type, qty change +/−, stock after, PO/style/designer).

Setup:

1. Create a Google Cloud project → enable **Google Sheets API** and **Google Drive API**.
2. Create a **service account**, download the JSON key.
3. Create a Google Sheet and share it with the service account `client_email` as **Editor**.
4. Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and paste the JSON plus the spreadsheet id.

Until this is configured, the app still runs; the sidebar shows that Sheets sync is skipped.

## Import files

- **BOM:** `style`, `material`, `qty_per_unit` (.xlsx or .csv). Re-import upserts on style+material using batch inserts.
- **Inventory:** `material`, `stock_qty`, optional `unit`. New materials get sequential `ITM-000001` codes that are never reused.

## Tabs

| Tab | Purpose |
| --- | --- |
| PO Deduction | Bulk grid (paste from Excel). Preview, then confirm. Shortfall warns but still deducts. |
| Designer Take | Pick inventory materials + qty, designer name, optional note. |
| Inventory | Live stock, searchable. |
| Adjustments | Plus or minus a quantity. Logged as history and synced to Sheets. |
| Consumption | Ranked usage with PO / designer contribution strings. |
| History | Every transaction, expandable to materials. |
| Export | One .xlsx with BOM, Inventory, Transactions, Consumption Summary, Item-wise Consumption. |
| Setup / Import | BOM/inventory upload and a single-item stock form. |
