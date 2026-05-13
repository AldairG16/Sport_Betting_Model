# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Automated soccer-betting model: Dixon-Coles + Ensemble (Poisson, ELO, xG, H2H, form) on top of PostgreSQL, with The Odds API for prices and Anthropic Claude + web_search for the pre-kickoff and pending-resolver agents. Production runs entirely on GitHub Actions cron; alerts go to Telegram.

## Common commands

```bash
# Run the daily pipeline modes (also wired into .github/workflows/*.yml)
python scripts/orchestrator.py --mode morning   # 06:00 MX — fetch odds + predict + notify
python scripts/orchestrator.py --mode closing   # 12:00 MX — capture closing odds for CLV
python scripts/orchestrator.py --mode evening   # 21:00 MX — results + CLV + nightly + preview
python scripts/orchestrator.py --mode results   # just re-fetch results and resolve bets
python scripts/orchestrator.py --mode weekly    # Mon — reload historical + calibration + walk-forward

# The two LLM-driven agents (separate from the orchestrator, separate workflows)
python scripts/pre_kickoff_analyst.py            # cron */15 from pre_kickoff.yml
python scripts/pre_kickoff_analyst.py --debug    # sanity check: 1 future bet, full pipeline, prints success/traceback
python scripts/resolve_pending_bets.py --hours-lag 6 --limit 15

# Tests
python -m pytest tests/ -v
python -m pytest tests/test_bet_filters.py -v      # one file
python -m pytest tests/test_kelly.py::test_kelly_caps -v   # one test

# Calibration audit (no API spend)
python scripts/audit_analyst_calibration.py --days 60
```

To trigger a workflow manually from GitHub UI: Actions → pick the workflow → "Run workflow". `pre_kickoff.yml` has a `debug_mode` checkbox that runs the analyst against any 1 pending future bet so the integration can be verified without spending tokens on a real cron cycle.

## Big picture

Three independent loops cooperate via PostgreSQL — no in-process state survives between runs. Each loop is its own GitHub Actions workflow under `.github/workflows/`.

**1. Daily pipeline (`orchestrator.py`, modes: morning/closing/evening/weekly)** — fetches odds from The Odds API, runs `src/pipeline/prediction_pipeline.py` to score every (match, market) pair, applies calibration + edge filters + Kelly sizing, inserts rows into `bets_history` with `result='pending'`. Evening mode then re-pulls results, runs `update_bet_results()` in `src/models/save_bets.py` to flip pending → win/loss, computes CLV, sends the day-summary to Telegram.

**2. Pre-kickoff analyst (`scripts/pre_kickoff_analyst.py`, `pre_kickoff.yml`)** — every 15 min, finds bets with `match_date` in the `[PRE_KICKOFF_WINDOW_MIN, PRE_KICKOFF_WINDOW_MAX]` future window (currently 30–60 min). Groups them by match, calls Anthropic API **once per match** (not per bet) with the quantitative dossier pre-computed from DB plus `data/analyst_lessons.md` + a learning memo built from `pre_kickoff_analyses ⋈ bets_history` of the last 30 d. The agent returns a verdict per market plus a single `best_pick`. Result is saved to `pre_kickoff_analyses` and sent to a dedicated Telegram channel (`TELEGRAM_BOT_TOKEN_PREKICKOFF`, falls back to main bot if unset). It **never modifies `bets_history`** — bets stay in their normal flow.

**3. Pending resolver (`scripts/resolve_pending_bets.py`, `resolve_pending.yml`)** — twice a day, takes bets stuck on `pending` >6 h after kickoff (typically corners/cards waiting on football-data.co.uk's 24–36 h CSV sync, or leagues The Odds API doesn't score like China). Groups by match, asks Claude to fetch full FT result via web_search, upserts the missing fields into `matches`, then calls `update_bet_results()` to flip the bets. Also `late_results.yml` re-runs `--mode results` at 00:00 and 06:30 MX to catch matches finishing after the evening pipeline.

The pipeline never deletes from `bets_history`. To audit "why did this not bet", check whether the row exists at all (model didn't score it) vs. exists with `result='pending'` (model bet, awaiting resolution) vs. exists as resolved. To audit "why didn't the analyst speak up", check `analyst_heartbeat` for the cron firing and `error_msg` for per-bet failures.

## Project-specific gotchas (these have all bitten us in production)

**GH Actions secrets expand to empty string when unset.** `int(os.environ.get("X", "default"))` returns `int("")` and raises `ValueError`, killing the pipeline before it writes anything. Always use `env_int` / `env_float` / `env_str` from `config/settings.py` for numeric env vars. The pattern is enforced by precedent — `src/utils/anthropic_budget.py` was the last new module to violate it and crashed every analyst run for a day.

**`bet["edge"]` is `edge_ev = prob*odds − 1`, not the real edge.** `bet["edge_market"] = prob − 1/odds` is the real edge. Filtering on `edge` lets longshots through and caused a −23 % ROI week. Always filter on `edge_market` (already done in `prediction_pipeline.py`, but keep the convention).

**Asian-handicap market keys are parameterized.** A bet's market is `ah_home_-0.5` but `MIN_EDGE_BY_MARKET` and `calibration_factors.json` are keyed by group: `ah_home_fav` / `ah_home_pk` / `ah_home_dog`. Use `_ah_group()` from `src/models/calibration_monitor.py` to map. There is precedent of a silent `dict.get(mkt, default)` falling to the default for every AH bet — see the patched lookup in `prediction_pipeline.py` around `_min_edge`.

**`upcoming_matches` accumulates stale duplicates.** Its upsert key includes `match_day` derived from kickoff, so when the API later corrects a kickoff time a brand-new row is inserted instead of updating. Any consumer must select with `DISTINCT ON (home_team_norm, away_team_norm, sport_key) … ORDER BY updated_at DESC` (already done in `prediction_pipeline.py`, `notify_telegram.py`, `pre_kickoff_analyst.py`). Cleanup runs at end of every `update_upcoming_matches.update_all()`.

**GH Actions cron drops `*/N` schedules at peak slots (`:00/:15/:30/:45`).** For anything that runs more often than hourly use off-peak minutes — `pre_kickoff.yml` uses `7,22,37,52 * * * *`. Symptom of falling into peak is heartbeats with `bets_found > 0, bets_analyzed = 0` for hours.

**Team names must go through `normalize_team()` before DB queries.** `matches` and `upcoming_matches` store normalized lowercase. Mismatches show up as bets stuck on `pending` forever even after `fetch_results` ran.

## Anthropic API usage and budget

Current credit balance is small (~$10). Two helpers in `src/utils/anthropic_budget.py` gate every call:

- `can_call(engine, estimated_cost_usd)` rejects the call if today's spend + estimate would exceed `ANTHROPIC_DAILY_BUDGET_USD` (default $0.30, override via GH secret).
- `record_call(engine, …)` is invoked after each call with the real `usage` from the response and writes to the `anthropic_usage` table.

Both `pre_kickoff_analyst.py` and `resolve_pending_bets.py` use `claude-haiku-4-5` with `max_uses=1–2` on `web_search_20250305`. Web search costs $0.01/search and dominates per-call cost. Per-call cost runs ≈$0.014–0.020. Don't introduce more web_search calls casually.

When the analyst crashes per-bet, the traceback is captured in `analyst_heartbeat.error_msg` (recent) and a Telegram alert fires if ≥3 bets in one run fail. Never re-add a bare `except Exception: pass` around the LLM call — that's how a day of silent failures happened.

## Calibration

`config/calibration_factors.json` is regenerated weekly by `step_calibration` (Monday). `apply_calibration(prob, market, league)` from `src/models/calibration_monitor.py` is the single entry point — it does isotonic-or-scalar correction with Bayesian smoothing (prior=30) and adaptive clamp (0.75–1.30 at n≥30, 0.85–1.20 at n<30). The `by_league` sub-dict is populated for any league with ≥25 bets in the 90-day window; `apply_calibration` checks league-level first then falls back to global, with the AH group fallback chain on top.

When a market crosses the alert thresholds (factor <0.82 or >1.20 with n≥10) the weekly cron sends `check_calibration_alert()` to Telegram. Treat those alerts as a signal to raise `MIN_EDGE_BY_MARKET` for that market until the bias data stabilizes — not necessarily to retrain. There's historical precedent: `ah_home_fav` / `ah_away_fav` triggered with factors ≈0.78 in May, the fix was both raising MIN_EDGE and fixing a lookup bug, no retrain.

## Subagents in this repo (`.claude/agents/`)

- `sports-betting-analyst` — interactive (you-invoke-it) qualitative review of today's bets. Different from the automated `pre_kickoff_analyst.py` script; that one is the production cron version.
- `model-auditor` — read-only weekly audit. Looks for data leakage, timezone bugs, bad calibration, silent errors. Returns 🟢/🟨/🟥 with a prioritized punch list.

## World Cup 2026 kill-switch

`soccer_fifa_world_cup` is auto-added by `_check_world_cup_activation()` at the orchestrator's startup once the date crosses 2026-06-11. The `WORLD_CUP_BETTING_ENABLED` env var defaults to `false` → Mundial picks are written to `data/paper_trades.jsonl` and tagged `[PAPER]` in Telegram, **not** inserted to `bets_history`. Levantar el kill-switch requires walk-forward 2022 ROI ≥ 0, ≥1 wk paper-trading, Brier ≤ 0.25 — checklist documented in `README.md`.

## Comments and language

Existing code is heavily commented in Spanish (project-historical), with English file/function names. Match the local style: prefer Spanish comments for new code in this codebase. Commit messages are in English.
