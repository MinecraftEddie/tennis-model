"""
Batch runner: runs all matches in config, ranks by EV edge, sends Telegram alerts.
Usage: py run_batch.py [config_path]
"""
import sys, os, json, logging, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
os.makedirs("data", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join("data", "model.log"), encoding="utf-8"),
    ],
)

cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
with open(cfg_path) as f:
    cfg = json.load(f)

import tennis_model.telegram as _tg
import tennis_model.formatter as _fmt

# Load telegram creds: prefer the supplied config, fall back to config.json in CWD
tg = cfg.get("telegram", {})
if not tg.get("bot_token"):
    fallback = "config.json"
    if os.path.exists(fallback) and fallback != cfg_path:
        with open(fallback) as _f:
            tg = json.load(_f).get("telegram", {})
if tg.get("bot_token"):  _tg.TELEGRAM_BOT_TOKEN   = tg["bot_token"]
if tg.get("chat_id"):    _tg.TELEGRAM_CHAT_ID     = str(tg["chat_id"])
if tg.get("edge_threshold"): _fmt.EDGE_ALERT_THRESHOLD = float(tg["edge_threshold"])

from tennis_model.pipeline import run_match
from tennis_model.odds_feed import print_odds_check

THRESHOLD = float(tg.get("edge_threshold", 7.0))

# ── odds-check pass ────────────────────────────────────────────────────────────
print("\n" + "═"*60)
print("ODDS CHECK (live API)")
print("═"*60)
for m in cfg.get("matches", []):
    parts = m["match"].split(" vs ", 1)
    if len(parts) == 2:
        print_odds_check(parts[0].strip(), parts[1].strip(), m.get("tour","wta"))

# ── run all matches ────────────────────────────────────────────────────────────
results = []
for i, m in enumerate(cfg.get("matches", []), 1):
    try:
        bk = m.get("bookmakers", [])
        if bk:
            odds_a, odds_b, bookmaker = bk[0].get("odds_a"), bk[0].get("odds_b"), bk[0].get("name","")
        else:
            odds_a, odds_b, bookmaker = m.get("odds_a"), m.get("odds_b"), m.get("bookmaker","")
        pick = run_match(
            m["match"], m.get("tournament","WTA Tour"), m.get("level","WTA 1000"),
            m.get("surface","Hard"), odds_a, odds_b, bookmaker, i,
            tour=m.get("tour","wta"),
            odds_timestamp=m.get("odds_timestamp",""),
        )
        best_edge = max(pick.edge_a or -999, pick.edge_b or -999)
        results.append((best_edge, pick))
        if i < len(cfg["matches"]):
            time.sleep(1)
    except Exception as exc:
        logging.error(f"Error on '{m.get('match','?')}': {exc}")

# ── ranked summary ─────────────────────────────────────────────────────────────
results.sort(key=lambda x: x[0], reverse=True)

print("\n" + "═"*60)
print(f"RANKED BY EDGE  (threshold {THRESHOLD}%)")
print("═"*60)
alerts_sent = 0
for rank, (edge, pick) in enumerate(results, 1):
    pa, pb    = pick.player_a, pick.player_b
    picked    = pick.require_picked_side()
    pick_name = picked["player"].short_name
    pick_odds = picked["market_odds"]
    edge_str = f"+{edge:.1f}%" if edge > 0 else f"{edge:.1f}%"
    # Alert already sent by maybe_alert() inside run_match() when is_value=True.
    # Use filter_reason to determine whether the alert actually fired.
    if not pick.filter_reason and edge >= THRESHOLD:
        flag = " 🚀 ALERT SENT"
        alerts_sent += 1
    elif pick.filter_reason:
        flag = f" ⛔ {pick.filter_reason}"
    elif edge > 0:
        flag = " ⚠️  BELOW THRESHOLD"
    else:
        flag = " ❌ NO EDGE"
    print(f"  {rank:2d}. {pa.short_name} vs {pb.short_name:<26} │ {edge_str:>7} │ Back {pick_name} @{pick_odds}{flag}")

print(f"\n{'═'*60}")
print(f"Total matches: {len(results)} │ Alerts sent: {alerts_sent} │ Threshold: {THRESHOLD}%")
print("═"*60)
