# 📊 Mutual Fund Portfolio Dashboard — v2

Premium-grade analytics for your full family mutual fund portfolio, built free and self-hostable. Inspired by the best features of **Groww**, **mProfit Pro**, **ET Money Genius**, **MF Central**, and **Paytm Money** — combined into one open dashboard.

---

## What's new in v2

**Free-tier feature parity** (matching what's free in the major apps):
- Multi-PAN, multi-email family view (consolidates many CAS PDFs)
- Asset-class allocation, sub-category breakdown
- AMC + scheme tables with cost, MV, gain, and XIRR
- Top/bottom performers, gain-coloured treemap
- Monthly cash-flow timeline with cumulative net invested

**Premium-tier feature parity** (these are usually paid in the big apps):
- 💼 **Capital Gains** (à la mProfit Pro) — FIFO-matched realized gains by FY, lot-level ITR-format export
- 🎯 **Tax Preview** (à la Groww Tax-asana) — adjustable rates, real-time what-if redemption tax
- ⚖️ **Allocation Drift** (à la ET Money Genius) — vs your target asset allocation, with rebalance amounts
- 🔄 **SIP Health** — active/cancelled SIPs, days since last instalment, stale-SIP alerts
- 📤 **Excel + JSON export** — full state, re-importable so you don't re-parse PDFs each time
- 💿 **Database persistence** — save portfolios to SQLite (default) or PostgreSQL; reload anytime without re-parsing PDFs

Built for India: Lakh/Crore formatting, FY 2024-25 tax regime by default, post-July 2024 LTCG rules.

---

## Quick start

```bash
pip install -r requirements.txt
streamlit run app.py
```

Opens at `http://localhost:8501`.

## Database configuration

By default the app uses **SQLite** and creates `mf_dashboard.db` in the project directory — no configuration needed.

For **PostgreSQL**, copy the example env file and fill in your connection string:

```bash
cp .env.example .env
# edit .env and set DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

SQLite works out of the box. PostgreSQL is ideal for multi-user or production deployments.

## Workflow

1. **Get your CAS PDFs** from one of:
   - [CAMS](https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement) — choose **Detailed**, type **CAS PDF**, single statement covering both CAMS and KFintech AMCs
   - [MF Central](https://mfcentral.com)
   - [NSDL CAS](https://nsdlcas.nsdl.com/)

2. **Upload** them in the sidebar. Multiple files combine automatically. Encrypted PDFs work — pass the PAN as password.

3. **Analyze** across 10 tabs: Overview · Family · By AMC · Schemes · Capital Gains · Tax Preview · Allocation · SIP Health · Cash Flows · Export

4. **Persist** — save portfolios to the database from the Export tab, or download Excel/JSON for offline use.

---

## Tax methodology

Capital gains use **FIFO** — purchases are matched against sales in the order they were made. This matches the SEBI/Income-Tax Department prescribed method for mutual funds.

Default tax rates (FY 2024-25 onwards, all editable in the Tax Preview tab):

| Asset class | Holding period | LTCG | STCG |
|---|---|---|---|
| Equity-oriented | >12 months | 12.5% over ₹1.25L exemption | 20% |
| Debt | >24 months | 12.5% (no indexation) | Slab rate (default 30%) |
| Hybrid | depends on equity % | Treated as equity by default | — |

Switches and lateral shifts between schemes **are** treated as taxable redemptions for capital-gains purposes (because they legally trigger a sale + new purchase), but are **excluded** from portfolio XIRR (because they're intra-portfolio movements with no external cash flow).

Stamp duty and STT are added to the cost basis of the lot they pair with on the same date.

---

## Deploy

### Streamlit Community Cloud (free, easiest)

1. Push this folder to a GitHub repo
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app** → point to your repo, main file `app.py`
3. Get a public URL like `https://yourname-mf-v2.streamlit.app`

> **Note:** Community Cloud is single-user by default. For multi-user access or database persistence, deploy to Render, Hugging Face Spaces, or a VPS.

### Render (free tier)

- Build command: `pip install -r requirements.txt`
- Start command: `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0`
- For PostgreSQL persistence, add a `.env` secret with your `DATABASE_URL`

### Docker

```bash
docker build -t mf-dashboard . && docker run -p 8501:8501 mf-dashboard
```

To use PostgreSQL in Docker, pass the connection string:

```bash
docker run -p 8501:8501 -e DATABASE_URL="postgresql://user:pass@host:5432/db" mf-dashboard
```

### Bare VPS

```bash
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
```

For production, run behind nginx with TLS termination.

---

## File structure

```
mf_dashboard_v2/
├── app.py                # Streamlit UI (10 tabs)
├── cas_parser.py         # PDF parser — captures units, price, SIP status
├── analytics.py          # FIFO capital gains, tax, drift, SIP analysis
├── database.py           # SQLAlchemy ORM — models, CRUD, session utilities
├── db_config.py         # Database config — SQLite default, PostgreSQL support
├── requirements.txt
├── Dockerfile
├── .env.example         # Template for DATABASE_URL
└── .streamlit/config.toml
```

---

## Privacy

- Files are processed **in Python session memory** only by default
- SQLite database (`mf_dashboard.db`) is local to the machine running the app
- For maximum privacy with financial data, run locally or on a VPS you control
- No telemetry, no external API calls, no data leaves your server

---

## What's not (yet) replicated

These features need **external data sources** beyond what's in the CAS:
- **Top underlying holdings** (Paytm Money) — needs scheme-portfolio data from AMFI / AMC monthly disclosures
- **Sector exposure** (Groww, Paytm) — same data dependency
- **Live NAV updates** — currently uses the NAV date in your CAS

If you want these, the path is: integrate the AMFI scheme-portfolio CSV (published monthly), match on ISIN, and aggregate across your holdings. Happy to add this in a future version.

## License

MIT — use, modify, deploy freely.