"""
Inventory Control Tower — hosted pipeline service.

This is the notebook's logic, converted into a plain Python service with one
HTTP endpoint that Make.com (or anything else) can call.

  load data -> deterministic agents -> Gemini reasoning -> Gemini email draft
  -> JSON response

Deploy this on Render. See README.md for setup.

Everything downstream of this JSON response -- writing to a Google Sheet for
Looker Studio, creating the Gmail draft, and triggering this endpoint on a
schedule -- is handled by Make.com, using Make's own built-in connectors.
That's deliberate: Make's Google Sheets and Gmail modules use a simple
"click to connect, log in" flow, not the Google Cloud Console. This service
doesn't touch Gmail or Sheets at all.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from google import genai

app = FastAPI(title="Inventory Control Tower Pipeline")

# ---------------------------------------------------------------------------
# Configuration -- from environment variables, set in Render's dashboard
# (Environment tab), not hardcoded.
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# Where the three source Excel files live -- included in the GitHub repo,
# in a folder named "data". See README.md for why this repo needs to be
# PRIVATE.
DATA_DIR = os.environ.get("DATA_DIR", "./data")

# Known, unresolved gap from the pilot: roughly two-thirds of Open PO material
# codes don't match the Stock SKU master, so Open PO Value understates true
# exposure. Returned in every response so it's never silently hidden.
OPEN_PO_MATCH_CAVEAT = (
    "Open PO Value reflects only material codes that matched the Stock SKU "
    "master. A material-code reconciliation gap was identified in the pilot "
    "and has not yet been resolved -- see the pilot notes."
)

FX_TO_INR = {"INR": 1.0, "USD": 95.6, "EUR": 110.3, "GBP": 129.4}  # update before reuse

genai_client = genai.Client(api_key=GEMINI_API_KEY)


class RunResponse(BaseModel):
    run_timestamp: str
    kpis: dict[str, Any]
    top_procurement_actions: list[dict[str, Any]]
    top_working_capital: list[dict[str, Any]]
    gemini_summary: dict[str, Any]
    email_subject: str
    email_body: str
    open_po_caveat: str


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = (
        df.columns.astype(str)
        .str.replace("\n", " ", regex=False)
        .str.replace("\r", " ", regex=False)
        .str.replace("\xa0", " ", regex=False)
        .str.strip()
    )
    return df


def clean_code(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.replace(r"\.0$", "", regex=True)


def find_file(keyword: str) -> str:
    import glob
    matches = [
        f for f in glob.glob(os.path.join(DATA_DIR, "*.xlsx"))
        if keyword.lower() in os.path.basename(f).lower()
    ]
    if not matches:
        raise FileNotFoundError(f"No workbook containing '{keyword}' found in {DATA_DIR}")
    return matches[0]


def load_data() -> dict[str, pd.DataFrame]:
    stock_file = find_file("stock")
    proc_file = find_file("procurement")

    stock_df = clean_columns(pd.read_excel(stock_file, sheet_name="Stock"))
    policy_df = clean_columns(pd.read_excel(stock_file, sheet_name="Inventory Policy"))
    proc_master_df = clean_columns(pd.read_excel(stock_file, sheet_name="Procurement data"))
    open_po_df = clean_columns(pd.read_excel(proc_file, sheet_name="Sheet1 (2)"))

    stock_df["SAP code"] = clean_code(stock_df["SAP code"])
    policy_df["SAP code"] = clean_code(policy_df["SAP code"])
    proc_master_df["SAP code"] = clean_code(proc_master_df["SAP code"])
    open_po_df["Material Code"] = clean_code(open_po_df["Material Code"])

    for col in ["Price_Unit", "WH Stock", "Sum of Plant Stock"]:
        stock_df[col] = pd.to_numeric(stock_df[col], errors="coerce")
    for col in ["Avg Consumption", "Min", "ROL", "MAX"]:
        policy_df[col] = pd.to_numeric(policy_df[col], errors="coerce")
    for col in ["Price_Unit", "LT in months", "Total In transit"]:
        proc_master_df[col] = pd.to_numeric(proc_master_df[col], errors="coerce")
    for col in ["Order Quantity", "Net Price", "Net Order Value",
                "Still to be delivered (qty)", "Still to be delivered (value)"]:
        open_po_df[col] = pd.to_numeric(open_po_df[col], errors="coerce")

    return {
        "stock": stock_df, "policy": policy_df, "proc_master": proc_master_df,
        "open_po": open_po_df,
    }


# ---------------------------------------------------------------------------
# Deterministic agents -- identical formulas to the notebook, with the
# currency-normalization fix applied to Open PO Value.
# ---------------------------------------------------------------------------

def build_master(data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    stock = data["stock"].copy()
    policy = data["policy"]
    pm = data["proc_master"]
    open_po_df = data["open_po"].copy()

    stock["Current Inventory"] = stock["WH Stock"].fillna(0) + stock["Sum of Plant Stock"].fillna(0)

    open_po_df["FX Rate"] = open_po_df["Currency"].map(FX_TO_INR)
    open_po_df["Still to be delivered (value, INR)"] = (
        open_po_df["Still to be delivered (value)"] * open_po_df["FX Rate"]
    )

    po_agg = (
        open_po_df.groupby("Material Code", dropna=False)
        .agg(
            Open_Qty=("Still to be delivered (qty)", "sum"),
            Open_PO_Value=("Still to be delivered (value, INR)", "sum"),
        )
        .reset_index()
    )

    master = (
        stock[["SAP code", "SKU Type", "Price_Unit", "Current Inventory"]]
        .merge(policy[["SAP code", "Avg Consumption", "Min", "ROL", "MAX"]], on="SAP code", how="left")
        .merge(pm[["SAP code", "LT in months", "Total In transit"]], on="SAP code", how="left")
        .merge(po_agg, left_on="SAP code", right_on="Material Code", how="left")
        .drop(columns=["Material Code"], errors="ignore")
    )

    master["Open_Qty"] = master["Open_Qty"].fillna(0)
    master["Open_PO_Value"] = master["Open_PO_Value"].fillna(0)
    for col in ["Avg Consumption", "Min", "ROL", "MAX", "LT in months"]:
        master[col] = pd.to_numeric(master[col], errors="coerce").fillna(0)

    master["Coverage_months"] = np.where(
        master["Avg Consumption"] > 0,
        (master["Current Inventory"] + master["Open_Qty"]) / master["Avg Consumption"],
        np.nan,
    )

    master["Inventory Health"] = np.select(
        [
            master["Current Inventory"] < master["Min"],
            master["Current Inventory"] < master["ROL"],
            master["Current Inventory"] > master["MAX"],
            master["Coverage_months"] > 12,
        ],
        ["Critical", "Replenishment Required", "Excess Inventory", "Slow Moving"],
        default="Healthy",
    )

    master["Recommended Order Qty"] = np.maximum(
        master["MAX"] - (master["Current Inventory"] + master["Open_Qty"]), 0
    )
    master["Transport Mode"] = np.where(
        master["Coverage_months"].notna() & (master["Coverage_months"] < (master["LT in months"] - 1)),
        "AIR", "SEA",
    )
    master["Excess Inventory"] = master["Current Inventory"] > master["MAX"]
    master["Slow Moving"] = master["Coverage_months"] > 12
    master["Stock-out Risk"] = master["Current Inventory"] < master["Min"]

    price_map = stock.set_index("SAP code")["Price_Unit"]
    master["Price_Unit"] = master["SAP code"].map(price_map)
    master["Inventory Value"] = master["Current Inventory"] * master["Price_Unit"]

    def priority(row):
        if row["Stock-out Risk"]:
            return 1
        if row["Current Inventory"] < row["ROL"]:
            return 2
        if row["Recommended Order Qty"] > 0:
            return 3
        return 4

    master["Priority"] = master.apply(priority, axis=1)
    return master


def build_procurement_actions(master: pd.DataFrame) -> pd.DataFrame:
    actions = master[(master["Priority"] <= 3) & (master["Recommended Order Qty"] > 0)].copy()
    actions = actions.sort_values(["Priority", "Coverage_months"], ascending=[True, True])
    actions["Risk Reason"] = np.select(
        [actions["Stock-out Risk"], actions["Current Inventory"] < actions["ROL"],
         actions["Recommended Order Qty"] > 0],
        ["Critical stock-out risk", "Below reorder level", "Future replenishment"],
        default="Review",
    )
    return actions[
        ["Priority", "SAP code", "SKU Type", "Coverage_months", "Current Inventory",
         "Open_Qty", "Recommended Order Qty", "LT in months", "Transport Mode", "Risk Reason"]
    ]


def build_working_capital(master: pd.DataFrame) -> pd.DataFrame:
    wc = master[(master["Excess Inventory"]) | (master["Slow Moving"])].copy()
    wc["Working Capital Action"] = np.select(
        [wc["Excess Inventory"] & wc["Slow Moving"], wc["Excess Inventory"], wc["Slow Moving"]],
        ["Stop/review future procurement; review inventory policy",
         "Review future procurement and MAX setting",
         "Review demand, inventory policy and slow-moving stock"],
        default="Review",
    )
    return wc[["SAP code", "Current Inventory", "MAX", "Coverage_months",
               "Excess Inventory", "Slow Moving", "Working Capital Action"]]


def compute_kpis(master: pd.DataFrame) -> dict[str, Any]:
    return {
        "Total Inventory Value": float(master["Inventory Value"].sum(min_count=1)),
        "Open PO Value": float(master["Open_PO_Value"].sum()),
        "Average Coverage (months)": float(master["Coverage_months"].mean()),
        "Critical / Stock-out Risk SKUs": int(master["Stock-out Risk"].sum()),
        "Excess Inventory Value": float(
            master.loc[master["Excess Inventory"], "Inventory Value"].sum(min_count=1)
        ),
        "AIR Cases": int((master["Transport Mode"] == "AIR").sum()),
    }


# ---------------------------------------------------------------------------
# Gemini reasoning
# ---------------------------------------------------------------------------

def gemini_json(prompt: str) -> dict[str, Any]:
    response = genai_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config={"temperature": 0.2, "response_mime_type": "application/json"},
    )
    text = response.text.strip()
    text = re.sub(r"^```json\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def build_gemini_summary(actions: pd.DataFrame, wc: pd.DataFrame) -> dict[str, Any]:
    acts = actions.head(15).replace({np.nan: None}).to_dict(orient="records")
    wc_cases = wc.head(15).replace({np.nan: None}).to_dict(orient="records")
    prompt = f"""
You are an inventory procurement planning assistant.

Use ONLY the supplied calculated facts. Do not invent quantities,
suppliers, lead times, costs, or business rules.

Return JSON with exactly these keys:
summary
top_risks
recommended_actions
data_quality_notes

Procurement actions:
{json.dumps(acts, default=str)}

Working capital cases:
{json.dumps(wc_cases, default=str)}
"""
    return gemini_json(prompt)


def build_email_draft(summary: dict[str, Any], actions: pd.DataFrame) -> dict[str, str]:
    acts = actions.head(10).replace({np.nan: None}).to_dict(orient="records")
    prompt = f"""
You are drafting an internal email to the Procurement team.

Use ONLY the supplied facts below. Do not invent numbers, suppliers, costs, or
commitments not present in the data.

Return JSON with exactly these keys:
subject: a short, specific subject line
body: the full email body as plain text, with a greeting and a sign-off
      placeholder like "[Your name]" -- do not sign it as Gemini or an AI.

Management summary: {json.dumps(summary, default=str)}
Top procurement actions: {json.dumps(acts, default=str)}
"""
    return gemini_json(prompt)


# ---------------------------------------------------------------------------
# The endpoint Make.com calls.
# ---------------------------------------------------------------------------

@app.post("/run", response_model=RunResponse)
def run_pipeline():
    try:
        run_timestamp = datetime.now(timezone.utc).isoformat()

        data = load_data()
        master = build_master(data)
        actions = build_procurement_actions(master)
        wc = build_working_capital(master)
        kpis = compute_kpis(master)
        summary = build_gemini_summary(actions, wc)
        draft = build_email_draft(summary, actions)

        return RunResponse(
            run_timestamp=run_timestamp,
            kpis=kpis,
            top_procurement_actions=actions.head(15).replace({np.nan: None}).to_dict(orient="records"),
            top_working_capital=wc.head(15).replace({np.nan: None}).to_dict(orient="records"),
            gemini_summary=summary,
            email_subject=draft["subject"],
            email_body=draft["body"],
            open_po_caveat=OPEN_PO_MATCH_CAVEAT,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """A free, zero-setup fallback dashboard -- no Google account, no Make.com,
    nothing external. Open this URL any time for a live view."""
    try:
        data = load_data()
        master = build_master(data)
        kpis = compute_kpis(master)
        actions = build_procurement_actions(master).head(10)
    except Exception as e:
        return HTMLResponse(f"<h1>Error loading data</h1><p>{e}</p>", status_code=500)

    def money(x):
        return f"₹{x:,.0f}"

    cards = [
        ("Total Inventory Value", money(kpis["Total Inventory Value"])),
        ("Open PO Value", money(kpis["Open PO Value"])),
        ("Average Coverage (months)", f"{kpis['Average Coverage (months)']:.2f}"),
        ("Critical / Stock-out Risk SKUs", str(kpis["Critical / Stock-out Risk SKUs"])),
        ("Excess Inventory Value", money(kpis["Excess Inventory Value"])),
        ("AIR Cases", str(kpis["AIR Cases"])),
    ]
    card_html = "".join(
        f'<div class="card"><div class="value">{val}</div><div class="label">{label}</div></div>'
        for label, val in cards
    )

    rows_html = "".join(
        f"<tr><td>{r['SAP code']}</td><td>{r['SKU Type']}</td>"
        f"<td>{r['Coverage_months']:.1f}</td><td>{int(r['Recommended Order Qty'])}</td>"
        f"<td>{r['Transport Mode']}</td><td>{r['Risk Reason']}</td></tr>"
        for r in actions.replace({np.nan: 0}).to_dict(orient="records")
    )

    html = f"""
    <html>
    <head>
      <title>Inventory Control Tower</title>
      <meta http-equiv="refresh" content="300">
      <style>
        body {{ font-family: -apple-system, Arial, sans-serif; background: #f5f6f8; margin: 0; padding: 32px; }}
        h1 {{ text-align: center; margin-bottom: 4px; }}
        .timestamp {{ text-align: center; color: #888; margin-bottom: 32px; font-size: 13px; }}
        .cards {{ display: flex; flex-wrap: wrap; gap: 20px; justify-content: center; margin-bottom: 40px; }}
        .card {{ background: white; border-radius: 12px; padding: 24px 32px; min-width: 200px;
                  text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
        .value {{ font-size: 28px; font-weight: 700; margin-bottom: 6px; }}
        .label {{ font-size: 13px; color: #666; }}
        table {{ width: 100%; max-width: 900px; margin: 0 auto; border-collapse: collapse;
                  background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }}
        th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid #eee; font-size: 14px; }}
        th {{ background: #fafafa; }}
        h2 {{ text-align: center; margin-top: 40px; }}
      </style>
    </head>
    <body>
      <h1>Inventory Control Tower</h1>
      <div class="timestamp">Refreshes automatically every 5 minutes -- last loaded {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>
      <div class="cards">{card_html}</div>
      <h2>Top Priority Actions</h2>
      <table>
        <tr><th>SAP Code</th><th>SKU Type</th><th>Coverage (mo)</th><th>Recommended Qty</th><th>Transport</th><th>Reason</th></tr>
        {rows_html}
      </table>
    </body>
    </html>
    """
    return HTMLResponse(html)
