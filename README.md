# SolPulse — Solana Ecosystem Auto-Updating Report & Interactive Dashboard

A zero-dependency, zero-API-key Solana ecosystem monitor. One Python file (stdlib only) pulls live data from Solana mainnet RPC, DeFiLlama, and CoinGecko's free endpoints, then generates three outputs:

- **Interactive HTML dashboard** (dark theme) — `reports/index.html`
- **Human-readable Markdown report** — `reports/report.md`
- **Machine-readable JSON** — `reports/report.json`

It auto-updates on a schedule via GitHub Actions (every 30 minutes) and deploys the dashboard to GitHub Pages — no server, no keys, no maintenance.

## Live demo

**https://Arun5768.github.io/solana-pulse/**

## What it covers

| Category | Metrics |
|---|---|
| Network performance | TPS (current + 30-min average), slot time, block height, slot, epoch + progress, total transaction count |
| Validator status | Active vs delinquent count, delinquency rate, top-10 stake concentration, total active stake, median commission, per-validator table (top 15 by stake) |
| Economic indicators | SOL price + 24h change, market cap, 24h volume, DeFi TVL, stablecoin supply, DEX volume (24h + 7d), median transaction fee |
| Supply | Total, circulating, % circulating |
| Ecosystem growth | Daily active signals via recent performance samples; TVL trend from history |
| Upgrades & news | Static watchlist of tracked developments (Alpenglow, SIMD-525) with links, refreshed each run |

## Anomaly detection

Each run compares current metrics against thresholds and rolling history (`reports/history.json`):

- **TPS drop/spike** — >40% deviation from the trailing average
- **Slow slots** — average slot time > 600 ms
- **Validator delinquency** — delinquency rate > 5%
- **Price moves** — |24h change| > 10%
- **TVL swings** — >8% change between runs

Anomalies render as alert cards on the dashboard, a section in the Markdown report, and an `anomalies` array in JSON.

## Data sources & integration

1. **Solana mainnet RPC** (`api.mainnet-beta.solana.com`) — `getEpochInfo`, `getRecentPerformanceSamples`, `getVoteAccounts`, `getSupply`, `getHealth`, `getVersion`, `getSlot`, `getBlockTime`. Direct JSON-RPC over stdlib `urllib`; no SDK.
2. **DeFiLlama** (free, no key) — chain TVL, stablecoin supply, DEX volumes.
3. **CoinGecko** (free, no key) — SOL price, market cap, volume, 24h change.

All fetches have timeouts and graceful degradation: if a source is down, the report still generates and marks the affected sections as unavailable.

## Automation strategy

- `docs/github-actions-update.yml` (copy it to `.github/workflows/update.yml` in your fork) runs `solpulse.py` on a cron (`*/30 * * * *`), commits the refreshed `reports/`, and deploys to GitHub Pages.
- Refresh interval is configurable by editing one cron line — or run locally with `--loop N` to regenerate every N seconds.
- `reports/history.json` persists a rolling window of past runs (committed by CI), powering trend deltas and anomaly baselines with no database.

## Run it yourself

Requires Python 3.8+. Nothing to install.

```bash
python3 solpulse.py             # one-shot: writes reports/ then exits
python3 solpulse.py --loop 1800 # regenerate every 30 minutes
python3 solpulse.py --rpc https://your-rpc.example.com  # custom RPC
```

Open `reports/index.html` in a browser. The dashboard is a self-contained static file (inline CSS/JS, no CDN).

## Repo layout

```
solpulse.py          # everything: fetch, analyze, render (stdlib only)
reports/
  index.html         # interactive dashboard (generated)
  report.md          # markdown report (generated)
  report.json        # structured data (generated)
  history.json       # rolling metric history for trends/anomalies
.github/workflows/
  update.yml         # 30-min cron: regenerate + deploy to Pages
```

## Interpreting the report

- Green/red accents on the dashboard mark healthy/degraded states.
- `report.json` top-level keys: `generated_at`, `network`, `validators`, `economics`, `supply`, `anomalies`, `warnings`, `sources`.
- `warnings` lists any data sources that failed this run; metrics from failed sources are `null` rather than stale.

## Enabling auto-updates (GitHub Actions)

Copy the template into place and push:

```bash
mkdir -p .github/workflows
cp docs/github-actions-update.yml .github/workflows/update.yml
git add .github && git commit -m "ci: enable auto-update" && git push
```

The workflow regenerates all reports every 30 minutes and redeploys the dashboard to GitHub Pages automatically.
