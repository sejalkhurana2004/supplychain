# Inventory Control Tower — GitHub + Render + Make.com + Looker Studio

No Google Cloud Console anywhere in this setup. Every account you'll touch has a
plain "sign in with Google" button, nothing more.

**The shape of the whole thing:**
Make.com wakes up on schedule → calls your Render URL → your service runs the
pipeline (data + math + Gemini) and returns JSON → Make.com writes a row to a
Google Sheet (for Looker Studio) and creates a Gmail draft — both using Make's
own built-in connectors, not your code.

---

## Step 1 — Put this code on GitHub

1. Go to github.com, create a **new repository**. **Make it Private** — this
   matters, because you're about to put your real inventory data in it.
2. Upload these files to it: `app.py`, `main.py`, `requirements.txt`,
   `Dockerfile`, and a `data/` folder containing your three Excel files
   (`Stock_Data.xlsx`, `Procurement_Data.xlsx`, `GRN_Data.xlsx`).
   - Easiest way if you're not comfortable with git commands: on the repo
     page, click **Add file → Upload files**, and drag everything in
     (including the `data` folder — GitHub's uploader supports folders).

That's it for GitHub — it's just where the code and data sit so Render can
find them.

---

## Step 2 — Deploy on Render

1. Go to render.com, sign up (GitHub login works directly, no separate account
   needed).
2. **New → Web Service.**
3. Connect your GitHub account, select the repository you just made.
4. Render will detect it's a Python app. Set:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python main.py`
   - (Not `uvicorn app:app --host 0.0.0.0 --port $PORT` — Render's Start
     Command field rejects `:` and `$`. `main.py` is a tiny wrapper that does
     the same thing without those characters in the field itself.)
5. Under **Environment**, add:
   - `GEMINI_API_KEY` = your Gemini API key
   - `DATABRICKS_HOST` = your workspace URL (same one from the notebook, e.g.
     `https://dbc-xxxxx.cloud.databricks.com`)
   - `DATABRICKS_TOKEN` = your Databricks token
   - `MLFLOW_EXPERIMENT_ID` = your experiment ID from the notebook
   - The last three are **optional** — if you skip them, the pipeline runs
     exactly the same, just without tracing. Since you already have these
     from the notebook, there's no reason not to include them.
6. Click **Create Web Service.** Render builds and deploys it — takes a few
   minutes the first time. When it's done, you get a URL like
   `https://inventory-control-tower.onrender.com`.

**Test it works:** open `https://your-render-url.onrender.com/dashboard` in a
browser. You should see the KPI cards and top actions table, computed live.
If this loads correctly, everything is working end to end already — Make.com
and Looker Studio are additions on top of a working thing, not a fix for a
broken one.

(Reminder: on Render's free tier, the service sleeps after 15 minutes idle and
takes 30-60 seconds to wake up on the next request — completely fine for a
once-a-day trigger, just don't be alarmed if the first load is slow.)

---

## Step 3 — Set up Make.com

1. Go to make.com, sign up.
2. **Create a new scenario.**
3. **Add module 1 — Schedule:** search for "Schedule," choose how often (e.g.
   daily at 6am). This replaces n8n's Schedule Trigger — same idea, Make's own
   version.
4. **Add module 2 — HTTP → Make a request:**
   - URL: `https://your-render-url.onrender.com/run`
   - Method: `POST`
   - This calls your pipeline and gets back the JSON with KPIs, procurement
     actions, and the drafted email subject/body.
5. **Add module 3 — Google Sheets → Add a row:**
   - Click **Add**, then **Connect a new account** — this opens a normal
     Google sign-in popup, you log in and approve, done. No developer
     console, no API key, no service account.
   - Point it at a new or existing Google Sheet (create one first at
     sheets.google.com with a tab named `KPI_History` and a header row:
     `Timestamp, Total Inventory Value, Open PO Value, Average Coverage,
     Critical SKUs, Excess Value, AIR Cases`).
   - Map each column to the matching field from module 2's HTTP response
     (Make shows you the available fields from the previous step — click into
     each column and pick the field, e.g. `kpis.Total Inventory Value`).
6. **Add module 4 — Gmail → Create a Draft:**
   - Same "connect a new account" flow — sign in with the Gmail account you
     want drafts to appear in.
   - **To:** the real procurement recipient's email address.
   - **Subject:** map to `email_subject` from module 2's response.
   - **Body:** map to `email_body` from module 2's response.
7. Click **Run once** to test the whole scenario manually before turning on
   the schedule. Check: did a row land in your Sheet? Did a draft appear in
   Gmail Drafts (not sent)?
8. Once that works, toggle the scenario **ON** — it now runs itself on the
   schedule from step 3, with no one present.

---

## Step 4 — Connect Looker Studio

1. Go to lookerstudio.google.com → **Create → Report.**
2. **Add data → Google Sheets** connector → select the same Sheet Make.com is
   writing to → select the `KPI_History` tab.
3. Build the dashboard:
   - **Scorecard** visuals for each KPI column (Total Inventory Value, Open PO
     Value, etc.) — set them to show the most recent row.
   - A **time-series chart** on the Timestamp column to show trends across
     runs.
4. Looker Studio refreshes on its own schedule (or click the refresh icon
   manually) — every time Make.com adds a new row, the dashboard picks it up.

---

## MLflow tracing (since you already have Databricks access)

With the three `DATABRICKS_*` / `MLFLOW_EXPERIMENT_ID` variables set on Render,
every `/run` call produces a full trace in your Databricks MLflow experiment —
same structure as your original notebook: one top-level "Inventory Control
Tower" span, with a nested span per agent, plus Gemini's own prompts/responses
autologged.

**One honest limitation:** if the Databricks host or token is wrong, the
service currently falls back to running untraced rather than crashing — which
is good for uptime, but means a broken tracing config fails silently. Check
your Databricks experiment after the first live run to confirm traces are
actually landing, rather than assuming they are.

## What each piece is actually doing, in one line each

- **GitHub** — just storage for your code and data, so Render has something to deploy.
- **Render** — runs your Python pipeline and gives it a URL that can be called anytime.
- **Make.com** — the alarm clock and the glue: wakes everything up on schedule, and connects the pipeline's output to Sheets and Gmail using its own simple logins.
- **Looker Studio** — reads the Sheet Make.com writes to, and draws the charts.

None of these four require anything beyond a normal sign-in. The Google Cloud
Console is not part of this setup anywhere.

---

## One thing to actually check before trusting this

Everything above proves the *automation* works. It does not fix the two open
data issues from the pilot: the Open PO reconciliation gap (still returned as
`open_po_caveat` in every response, on purpose) and the unused GRN
validation. Automating a pipeline doesn't resolve open questions about its
numbers — it just means those numbers now update on their own.
