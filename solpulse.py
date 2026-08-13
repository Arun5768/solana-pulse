#!/usr/bin/env python3
"""SolPulse — Solana Ecosystem Auto-Updating Report & Interactive Dashboard.

Zero external dependencies: Python 3.8+ standard library only.
Data sources: Solana mainnet RPC (no key), DeFiLlama (no key), CoinGecko (no key).

Usage:
  python3 solpulse.py                 # generate one report (JSON + Markdown + HTML)
  python3 solpulse.py --loop          # regenerate forever at the configured interval
  python3 solpulse.py --serve         # loop + serve the dashboard over HTTP
  python3 solpulse.py --interval 300  # override refresh interval (seconds)
"""

import argparse
import json
import os
import statistics
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(BASE_DIR, "reports")
HISTORY_PATH = os.path.join(REPORT_DIR, "history.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULT_CONFIG = {
    "rpc_url": "https://api.mainnet-beta.solana.com",
    "refresh_interval_seconds": 600,
    "history_max_points": 288,
    "top_validators": 10,
    "anomaly_thresholds": {
        "tps_drop_pct": 40,
        "slot_time_ms_max": 600,
        "delinquent_pct_max": 5.0,
        "sol_price_move_pct": 8.0,
        "tvl_move_pct": 10.0,
    },
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[warn] bad config.json ({e}); using defaults")
    return cfg


def http_json(url, payload=None, timeout=15):
    headers = {"Content-Type": "application/json", "User-Agent": "SolPulse/1.0"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def rpc(cfg, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    out = http_json(cfg["rpc_url"], body)
    if "error" in out:
        raise RuntimeError(f"RPC {method}: {out['error']}")
    return out["result"]


def safe(fn, label, errors):
    try:
        return fn()
    except Exception as e:
        errors.append(f"{label}: {e}")
        return None


# ---------------------------------------------------------------- collectors

def collect_network(cfg, errors):
    net = {}
    epoch = safe(lambda: rpc(cfg, "getEpochInfo"), "getEpochInfo", errors)
    if epoch:
        net["epoch"] = epoch["epoch"]
        net["slot"] = epoch["absoluteSlot"]
        net["block_height"] = epoch["blockHeight"]
        net["epoch_progress_pct"] = round(100 * epoch["slotIndex"] / epoch["slotsInEpoch"], 2)
        net["total_transactions"] = epoch.get("transactionCount")

    perf = safe(lambda: rpc(cfg, "getRecentPerformanceSamples", [30]), "getRecentPerformanceSamples", errors)
    if perf:
        tps = [s["numTransactions"] / s["samplePeriodSecs"] for s in perf if s["samplePeriodSecs"]]
        slots = [s["numSlots"] / s["samplePeriodSecs"] for s in perf if s["samplePeriodSecs"]]
        if tps:
            net["tps_current"] = round(tps[0], 1)
            net["tps_avg_30m"] = round(statistics.mean(tps), 1)
            net["tps_series"] = [round(x, 1) for x in reversed(tps)]
        if slots and statistics.mean(slots) > 0:
            net["slot_time_ms"] = round(1000 / statistics.mean(slots), 1)

    health = safe(lambda: rpc(cfg, "getHealth"), "getHealth", errors)
    net["rpc_health"] = health if health else "unknown"

    version = safe(lambda: rpc(cfg, "getVersion"), "getVersion", errors)
    if version:
        net["node_version"] = version.get("solana-core")

    slot = net.get("slot")
    if slot:
        bt = safe(lambda: rpc(cfg, "getBlockTime", [slot]), "getBlockTime", errors)
        if bt:
            net["latest_block_time_utc"] = datetime.fromtimestamp(bt, tz=timezone.utc).isoformat()
    return net


def collect_validators(cfg, errors):
    va = safe(lambda: rpc(cfg, "getVoteAccounts"), "getVoteAccounts", errors)
    if not va:
        return {}
    current, delinquent = va.get("current", []), va.get("delinquent", [])
    total = len(current) + len(delinquent)
    stake_total = sum(v["activatedStake"] for v in current + delinquent)
    top = sorted(current, key=lambda v: -v["activatedStake"])[: cfg["top_validators"]]
    top_stake = sum(v["activatedStake"] for v in top)
    commissions = [v["commission"] for v in current]
    return {
        "active": len(current),
        "delinquent": len(delinquent),
        "total": total,
        "delinquent_pct": round(100 * len(delinquent) / total, 2) if total else 0,
        "total_stake_sol": round(stake_total / 1e9),
        "top_n": cfg["top_validators"],
        "top_n_stake_pct": round(100 * top_stake / stake_total, 2) if stake_total else 0,
        "median_commission_pct": statistics.median(commissions) if commissions else None,
        "top_validators": [
            {
                "vote_pubkey": v["votePubkey"],
                "stake_sol": round(v["activatedStake"] / 1e9),
                "stake_pct": round(100 * v["activatedStake"] / stake_total, 2) if stake_total else 0,
                "commission_pct": v["commission"],
            }
            for v in top
        ],
    }


def collect_supply(cfg, errors):
    sup = safe(lambda: rpc(cfg, "getSupply", [{"excludeNonCirculatingAccountsList": True}]), "getSupply", errors)
    if not sup:
        return {}
    v = sup["value"]
    return {
        "total_sol": round(v["total"] / 1e9),
        "circulating_sol": round(v["circulating"] / 1e9),
        "non_circulating_sol": round(v["nonCirculating"] / 1e9),
        "circulating_pct": round(100 * v["circulating"] / v["total"], 2),
    }


def collect_economics(errors):
    eco = {}
    cg = safe(
        lambda: http_json(
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=solana&vs_currencies=usd&include_market_cap=true"
            "&include_24hr_vol=true&include_24hr_change=true"
        ),
        "coingecko", errors,
    )
    if cg and "solana" in cg:
        s = cg["solana"]
        eco["sol_price_usd"] = s.get("usd")
        eco["market_cap_usd"] = s.get("usd_market_cap")
        eco["volume_24h_usd"] = s.get("usd_24h_vol")
        eco["price_change_24h_pct"] = round(s.get("usd_24h_change", 0), 2)

    chains = safe(lambda: http_json("https://api.llama.fi/v2/chains"), "defillama-chains", errors)
    if chains:
        sol = next((c for c in chains if c.get("name") == "Solana"), None)
        if sol:
            eco["tvl_usd"] = round(sol["tvl"])

    stables = safe(lambda: http_json("https://stablecoins.llama.fi/stablecoinchains"), "defillama-stables", errors)
    if stables:
        sol = next((c for c in stables if c.get("name") == "Solana"), None)
        if sol:
            total = sum(v for v in sol.get("totalCirculatingUSD", {}).values() if isinstance(v, (int, float)))
            eco["stablecoin_supply_usd"] = round(total)

    dex = safe(
        lambda: http_json("https://api.llama.fi/overview/dexs/solana?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true"),
        "defillama-dex", errors,
    )
    if dex:
        eco["dex_volume_24h_usd"] = dex.get("total24h")
        eco["dex_volume_7d_usd"] = dex.get("total7d")
        eco["dex_protocol_count"] = len(dex.get("protocols", []))
    return eco


def collect_ecosystem_notes():
    return {
        "upcoming": [
            {
                "name": "Alpenglow",
                "summary": "Consensus overhaul (Votor + Rotor) replacing TowerBFT/Proof-of-History voting; targets ~150ms finality.",
                "reference": "https://www.anza.xyz/blog/alpenglow-a-new-consensus-for-solana",
            },
            {
                "name": "SIMD-0326 / SIMD-525 track",
                "summary": "Ongoing SIMD governance proposals; monitor the solana-improvement-documents repo for status.",
                "reference": "https://github.com/solana-foundation/solana-improvement-documents",
            },
        ],
        "watch": [
            "Tokenized equities volume (xStocks et al.) as an ecosystem growth signal",
            "Firedancer validator client adoption share",
        ],
    }


# ------------------------------------------------------------------ history

def load_history():
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def append_history(history, snapshot, cfg):
    history.append(snapshot)
    history[:] = history[-cfg["history_max_points"]:]
    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f)


# ---------------------------------------------------------------- anomalies

def detect_anomalies(report, history, cfg):
    th = cfg["anomaly_thresholds"]
    found = []
    net, val, eco = report["network"], report["validators"], report["economics"]

    tps_now, tps_avg = net.get("tps_current"), net.get("tps_avg_30m")
    if tps_now is not None and tps_avg:
        drop = 100 * (tps_avg - tps_now) / tps_avg
        if drop >= th["tps_drop_pct"]:
            found.append({"severity": "high", "metric": "tps",
                          "message": f"TPS dropped {drop:.0f}% vs 30m average ({tps_now} vs {tps_avg})"})

    st = net.get("slot_time_ms")
    if st and st > th["slot_time_ms_max"]:
        found.append({"severity": "medium", "metric": "slot_time",
                      "message": f"Slow slots: {st}ms average (threshold {th['slot_time_ms_max']}ms)"})

    dp = val.get("delinquent_pct")
    if dp is not None and dp > th["delinquent_pct_max"]:
        found.append({"severity": "high", "metric": "validators",
                      "message": f"Validator delinquency at {dp}% (threshold {th['delinquent_pct_max']}%)"})

    pc = eco.get("price_change_24h_pct")
    if pc is not None and abs(pc) >= th["sol_price_move_pct"]:
        found.append({"severity": "medium", "metric": "sol_price",
                      "message": f"SOL moved {pc:+.1f}% in 24h (threshold ±{th['sol_price_move_pct']}%)"})

    if history and eco.get("tvl_usd"):
        prev = next((h.get("tvl_usd") for h in reversed(history) if h.get("tvl_usd")), None)
        if prev:
            move = 100 * (eco["tvl_usd"] - prev) / prev
            if abs(move) >= th["tvl_move_pct"]:
                found.append({"severity": "medium", "metric": "tvl",
                              "message": f"TVL moved {move:+.1f}% since last snapshot"})

    if net.get("rpc_health") not in ("ok", "unknown"):
        found.append({"severity": "high", "metric": "rpc_health",
                      "message": f"RPC health check returned: {net.get('rpc_health')}"})
    return found


# ------------------------------------------------------------------ outputs

def fmt_usd(n):
    if n is None:
        return "—"
    if n >= 1e9:
        return f"${n / 1e9:.2f}B"
    if n >= 1e6:
        return f"${n / 1e6:.1f}M"
    return f"${n:,.0f}"


def write_markdown(report):
    net, val, sup, eco = report["network"], report["validators"], report["supply"], report["economics"]
    lines = [
        "# Solana Ecosystem Pulse Report",
        "",
        f"Generated: **{report['generated_at_utc']}** · refresh interval {report['refresh_interval_seconds']}s · sources: Solana RPC, DeFiLlama, CoinGecko (no API keys)",
        "",
        "## Network Performance",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Current TPS | {net.get('tps_current', '—')} |",
        f"| Avg TPS (30m) | {net.get('tps_avg_30m', '—')} |",
        f"| Slot time | {net.get('slot_time_ms', '—')} ms |",
        f"| Slot / Block height | {net.get('slot', '—'):,} / {net.get('block_height', 0):,} |",
        f"| Epoch | {net.get('epoch', '—')} ({net.get('epoch_progress_pct', '—')}% complete) |",
        f"| Total transactions | {net.get('total_transactions', 0):,} |",
        f"| RPC health / node version | {net.get('rpc_health', '—')} / {net.get('node_version', '—')} |",
        "",
        "## Validators",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Active / Delinquent | {val.get('active', '—')} / {val.get('delinquent', '—')} ({val.get('delinquent_pct', '—')}% delinquent) |",
        f"| Total stake | {val.get('total_stake_sol', 0):,} SOL |",
        f"| Top {val.get('top_n', 10)} stake share | {val.get('top_n_stake_pct', '—')}% |",
        f"| Median commission | {val.get('median_commission_pct', '—')}% |",
        "",
        "### Top validators by stake",
        "",
        "| Vote account | Stake (SOL) | Share | Commission |",
        "|---|---|---|---|",
    ]
    for v in val.get("top_validators", []):
        lines.append(f"| `{v['vote_pubkey'][:20]}…` | {v['stake_sol']:,} | {v['stake_pct']}% | {v['commission_pct']}% |")
    lines += [
        "",
        "## Economics",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| SOL price | ${eco.get('sol_price_usd', '—')} ({eco.get('price_change_24h_pct', 0):+.1f}% 24h) |",
        f"| Market cap | {fmt_usd(eco.get('market_cap_usd'))} |",
        f"| 24h volume | {fmt_usd(eco.get('volume_24h_usd'))} |",
        f"| DeFi TVL | {fmt_usd(eco.get('tvl_usd'))} |",
        f"| Stablecoin supply | {fmt_usd(eco.get('stablecoin_supply_usd'))} |",
        f"| DEX volume (24h / 7d) | {fmt_usd(eco.get('dex_volume_24h_usd'))} / {fmt_usd(eco.get('dex_volume_7d_usd'))} |",
        "",
        "## Supply",
        "",
        f"- Circulating: **{sup.get('circulating_sol', 0):,} SOL** ({sup.get('circulating_pct', '—')}% of {sup.get('total_sol', 0):,} total)",
        "",
        "## Anomalies",
        "",
    ]
    if report["anomalies"]:
        for a in report["anomalies"]:
            lines.append(f"- **[{a['severity'].upper()}] {a['metric']}** — {a['message']}")
    else:
        lines.append("- None detected. All monitored metrics within thresholds.")
    lines += ["", "## Upcoming Developments", ""]
    for u in report["ecosystem"]["upcoming"]:
        lines.append(f"- **{u['name']}** — {u['summary']} ([ref]({u['reference']}))")
    if report.get("collection_errors"):
        lines += ["", "## Collection warnings", ""]
        for e in report["collection_errors"]:
            lines.append(f"- {e}")
    with open(os.path.join(REPORT_DIR, "report.md"), "w") as f:
        f.write("\n".join(lines) + "\n")


def write_dashboard(report, history):
    payload = json.dumps({"report": report, "history": history})
    html = DASHBOARD_TEMPLATE.replace("__DATA__", payload)
    with open(os.path.join(REPORT_DIR, "dashboard.html"), "w") as f:
        f.write(html)
    with open(os.path.join(REPORT_DIR, "index.html"), "w") as f:
        f.write(html)


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="600">
<title>SolPulse — Solana Ecosystem Dashboard</title>
<style>
:root{--bg:#0b0e14;--card:#131826;--border:#1f2740;--fg:#e6e9f2;--muted:#8b93ab;--accent:#9945FF;--accent2:#14F195;--warn:#f5a623;--bad:#ff5c7a}
*{box-sizing:border-box;margin:0}body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,'Segoe UI',Roboto,sans-serif;padding:28px}
.wrap{max-width:1180px;margin:0 auto}h1{font-size:26px;letter-spacing:-.5px}
h1 .dot{color:var(--accent2)}.sub{color:var(--muted);margin:6px 0 22px;font-size:13px}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fit,minmax(215px,1fr));margin-bottom:22px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.k{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.6px}
.v{font-size:24px;font-weight:700;margin-top:4px}.s{color:var(--muted);font-size:12px;margin-top:3px}
.pos{color:var(--accent2)}.neg{color:var(--bad)}
section{margin-bottom:26px}h2{font-size:16px;margin-bottom:10px;color:#c4cbe0}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;font-size:13px}
th,td{padding:9px 13px;text-align:left;border-bottom:1px solid var(--border)}th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
tr:last-child td{border-bottom:0}code{color:var(--accent2);font-size:12px}
.anom{border-left:3px solid var(--warn);background:var(--card);border-radius:8px;padding:10px 14px;margin-bottom:8px;font-size:13px}
.anom.high{border-left-color:var(--bad)}.ok{color:var(--accent2)}
.bar{height:8px;background:#1c2338;border-radius:6px;overflow:hidden;margin-top:8px}
.bar i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2))}
svg{width:100%;height:70px;margin-top:8px}.foot{color:var(--muted);font-size:12px;margin-top:26px}
a{color:var(--accent2)}
</style></head><body><div class="wrap">
<h1>SolPulse<span class="dot">●</span> <span style="font-weight:400;color:var(--muted);font-size:16px">Solana Ecosystem Dashboard</span></h1>
<p class="sub" id="sub"></p>
<div class="grid" id="cards"></div>
<section id="anoms-sec"><h2>Anomaly Detection</h2><div id="anoms"></div></section>
<section><h2>TPS (last 30 minutes)</h2><div class="card"><svg id="spark" viewBox="0 0 600 70" preserveAspectRatio="none"></svg></div></section>
<section><h2>Top Validators by Stake</h2><table id="vals"><thead><tr><th>#</th><th>Vote account</th><th>Stake (SOL)</th><th>Share</th><th>Commission</th></tr></thead><tbody></tbody></table></section>
<section><h2>Upcoming Developments</h2><table id="devs"><thead><tr><th>Item</th><th>Summary</th></tr></thead><tbody></tbody></table></section>
<p class="foot">Auto-refreshes every 10 min. Data: Solana mainnet RPC · DeFiLlama · CoinGecko — no API keys. Built with Python stdlib. <a href="report.md">Markdown report</a> · <a href="report.json">JSON</a></p>
</div>
<script>
const D=__DATA__;const r=D.report,n=r.network,v=r.validators,e=r.economics,s=r.supply;
const usd=x=>x==null?"—":x>=1e9?"$"+(x/1e9).toFixed(2)+"B":x>=1e6?"$"+(x/1e6).toFixed(1)+"M":"$"+Math.round(x).toLocaleString();
document.getElementById("sub").textContent="Generated "+r.generated_at_utc+" · RPC health: "+n.rpc_health+" · node "+(n.node_version||"—");
const chg=e.price_change_24h_pct||0;
const cards=[
["SOL Price","$"+(e.sol_price_usd??"—"),(chg>=0?"+":"")+chg+"% 24h",chg>=0?"pos":"neg"],
["Market Cap",usd(e.market_cap_usd),"24h vol "+usd(e.volume_24h_usd)],
["DeFi TVL",usd(e.tvl_usd),"Stablecoins "+usd(e.stablecoin_supply_usd)],
["DEX Volume 24h",usd(e.dex_volume_24h_usd),"7d "+usd(e.dex_volume_7d_usd)],
["Current TPS",n.tps_current??"—","30m avg "+(n.tps_avg_30m??"—")],
["Slot Time",(n.slot_time_ms??"—")+" ms","Slot "+(n.slot??0).toLocaleString()],
["Epoch "+(n.epoch??"—"),(n.epoch_progress_pct??"—")+"%","progress",null,n.epoch_progress_pct],
["Validators",v.active??"—",(v.delinquent??"—")+" delinquent ("+(v.delinquent_pct??"—")+"%)",(v.delinquent_pct>5?"neg":"pos")],
["Total Stake",((v.total_stake_sol??0)/1e6).toFixed(1)+"M SOL","Top "+(v.top_n||10)+" hold "+(v.top_n_stake_pct??"—")+"%"],
["Circulating",((s.circulating_sol??0)/1e6).toFixed(1)+"M SOL",(s.circulating_pct??"—")+"% of supply"],
["Total Txns",((n.total_transactions??0)/1e9).toFixed(1)+"B","all-time"],
["Median Commission",(v.median_commission_pct??"—")+"%","across active validators"]];
document.getElementById("cards").innerHTML=cards.map(c=>`<div class="card"><div class="k">${c[0]}</div><div class="v ${c[3]||""}">${c[1]}</div><div class="s">${c[2]||""}</div>${c[4]!=null?`<div class="bar"><i style="width:${c[4]}%"></i></div>`:""}</div>`).join("");
const an=r.anomalies||[];
document.getElementById("anoms").innerHTML=an.length?an.map(a=>`<div class="anom ${a.severity}"><b>[${a.severity.toUpperCase()}] ${a.metric}</b> — ${a.message}</div>`).join(""):'<div class="card ok">✓ No anomalies detected — all monitored metrics within thresholds.</div>';
const ts=n.tps_series||[];
if(ts.length>1){const mx=Math.max(...ts),mn=Math.min(...ts),rg=mx-mn||1;
const pts=ts.map((t,i)=>`${(i/(ts.length-1))*600},${65-((t-mn)/rg)*58}`).join(" ");
document.getElementById("spark").innerHTML=`<polyline points="${pts}" fill="none" stroke="#14F195" stroke-width="2"/><text x="4" y="12" fill="#8b93ab" font-size="10">max ${mx}</text><text x="4" y="66" fill="#8b93ab" font-size="10">min ${mn}</text>`;}
document.querySelector("#vals tbody").innerHTML=(v.top_validators||[]).map((x,i)=>`<tr><td>${i+1}</td><td><code>${x.vote_pubkey}</code></td><td>${x.stake_sol.toLocaleString()}</td><td>${x.stake_pct}%</td><td>${x.commission_pct}%</td></tr>`).join("");
document.querySelector("#devs tbody").innerHTML=(r.ecosystem.upcoming||[]).map(u=>`<tr><td><a href="${u.reference}">${u.name}</a></td><td>${u.summary}</td></tr>`).join("");
</script></body></html>
"""


def generate(cfg):
    os.makedirs(REPORT_DIR, exist_ok=True)
    errors = []
    report = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "refresh_interval_seconds": cfg["refresh_interval_seconds"],
        "network": collect_network(cfg, errors),
        "validators": collect_validators(cfg, errors),
        "supply": collect_supply(cfg, errors),
        "economics": collect_economics(errors),
        "ecosystem": collect_ecosystem_notes(),
        "collection_errors": errors,
    }
    history = load_history()
    report["anomalies"] = detect_anomalies(report, history, cfg)
    snapshot = {
        "t": report["generated_at_utc"],
        "tps": report["network"].get("tps_current"),
        "sol_price_usd": report["economics"].get("sol_price_usd"),
        "tvl_usd": report["economics"].get("tvl_usd"),
        "delinquent_pct": report["validators"].get("delinquent_pct"),
    }
    append_history(history, snapshot, cfg)
    with open(os.path.join(REPORT_DIR, "report.json"), "w") as f:
        json.dump(report, f, indent=2)
    write_markdown(report)
    write_dashboard(report, history)
    print(f"[ok] report generated at {report['generated_at_utc']} "
          f"({len(errors)} warnings, {len(report['anomalies'])} anomalies)")
    return report


def serve(port):
    os.chdir(REPORT_DIR)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="SolPulse — Solana ecosystem auto-updating report")
    ap.add_argument("--loop", action="store_true", help="regenerate at the configured interval forever")
    ap.add_argument("--serve", action="store_true", help="loop + serve dashboard over HTTP")
    ap.add_argument("--interval", type=int, help="override refresh interval in seconds")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    args = ap.parse_args()

    cfg = load_config()
    if args.interval:
        cfg["refresh_interval_seconds"] = args.interval

    generate(cfg)
    if args.serve:
        threading.Thread(target=serve, args=(args.port,), daemon=True).start()
        print(f"[ok] serving dashboard on port {args.port}")
    if args.loop or args.serve:
        while True:
            time.sleep(cfg["refresh_interval_seconds"])
            try:
                generate(cfg)
            except Exception as e:
                print(f"[warn] generation failed: {e}")


if __name__ == "__main__":
    main()
