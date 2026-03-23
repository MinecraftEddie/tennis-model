# Tennis Model

## Run Commands
```bash
# Run from the parent directory of tennis_model/ (PYTHONPATH must include project root)
python tennis_model/cli.py --scan-today                          # full slate scan
python tennis_model/cli.py --match "A vs B" --market_odds 1.80 2.10 --surface Hard
python tennis_model/cli.py --results                             # backtest report
python tennis_model/cli.py --record <pred_id> <winner>          # record result
python tennis_model/cli.py --closing-odds <pred_id> <oa> <ob>   # record CLV
```

## Key Files
```
tennis_model/
├── cli.py              # entry point
├── pipeline.py         # orchestrator: run_match(), scan_today(), run_from_config()
├── models.py           # PlayerProfile, MatchPick, SERVE_BOUNDS dataclasses
├── profiles.py         # STATIC_PROFILES, WTA_PROFILES, PLAYER_ID_MAP
├── config.json         # match config (tournament, surface, odds, players)
├── ingestion/
│   └── tennis_abstract.py  # ATP matchmx + WTA jsfrags parsers
└── evaluator/
    └── rules.py        # second-pass filter rules
data/
├── predictions.json    # all stored predictions + results
└── elo_ratings.json    # persisted ELO ratings
```

## Data Conventions
- Prediction IDs: `YYYY-MM-DD_lastname_a_lastname_b`
- `profit_loss`: net units per 1 unit staked (win: odds−1, loss: −1, void: 0)
- `edge_a/b`: stored as decimal fraction (0.15 = 15%)
- `result`: `A_WIN` | `B_WIN` | `VOID` | `null` (pending)
- PLAYER_ID_MAP key: `"lastname"` → `("Full Name", "url-slug", "ATPID")`
- WTA players use jsfrags (tennis_abstract_dynamic); stale static profiles are gated

## Module Overview
- `model.py` — factor weights → win probability
- `elo.py` — surface-aware ELO ratings
- `hold_break.py` — Markov serve/return model
- `monte_carlo.py` — match simulation
- `confidence.py` — HIGH/MEDIUM/LOW tier classification
- `ev.py` — expected value filter
- `evaluator/evaluator.py` — second-pass quality gate (CLEAN/CAUTION/FRAGILE)
- `formatter.py` — pick card + quality tier output
- `telegram.py` — alert delivery
- `backtest.py` — predictions store, result recording, P&L report
- `odds_feed.py` — The Odds API (fetch_slate, get_live_odds)

## Conventions Code
- Tours : `"ATP"` | `"WTA"` — config dans `tour_config.py`
- Seuils de confiance : voir `confidence.py` (ne pas hardcoder ailleurs)
- Nouveaux joueurs : ajouter dans `profiles.py` → `PLAYER_ID_MAP`

## À ne pas toucher sans raison
- `models.py` : dataclasses stables, tout le pipeline en dépend
- `data/predictions.json` : ne jamais éditer à la main, passer par `--record`
