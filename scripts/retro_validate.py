"""
scripts/retro_validate.py
=========================
VALIDACIÓN RETROSPECTIVA del modelo recalibrado (xG 0.28/0.03, ventana 10,
decay 0.80) sobre bets ya resueltas.

Para cada bet resuelta:
  1. Reconstruye los lambdas del modelo NUEVO usando SOLO datos previos al
     partido (form con cutoff_date, xG replicado con filtro temporal)
  2. Calcula la probabilidad nueva del mercado de la bet
  3. Determina si el modelo nuevo la habría apostado (edge >= 5% vs la odd
     guardada)
  4. Usa el resultado REAL ya conocido para simular ganancia/pérdida

Al final compara:
  - Modelo VIEJO: lo que realmente pasó (WR, ROI) en las mismas bets
  - Modelo NUEVO: WR/ROI simulados sobre las que habría apostado
  - Brecha de calibración de cada uno

LIMITACIONES honestas:
  - Excluye MLE/clima/motivación (aproximación del pipeline completo)
  - H2H sin corte temporal (leve leakage en 15% del blend)
  - Requiere shots históricos: bets de ligas sin shots quedan fuera
"""

import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.append(str(Path(__file__).parent.parent))

import pandas as pd
from sqlalchemy import text

from config.database import engine
from src.features.team_form import get_team_form
from src.features.league_calibration import get_lambda_multipliers, get_over25_rate
from src.features.h2h_stats import get_h2h_stats
from src.models.poisson_markets import totals_and_btts
from src.models.dixon_coles_model import match_outcomes

PROFIT_FACTOR = {"win": 1.0, "half_win": 0.5, "push": 0.0, "half_loss": -0.5, "loss": -1.0}


def xg_asof(team, cutoff, window=10, decay=0.80):
    df = pd.read_sql(text("""
        SELECT date, home_team, away_team, home_shots, away_shots,
               home_shots_target, away_shots_target
        FROM matches
        WHERE (LOWER(home_team)=LOWER(:t) OR LOWER(away_team)=LOWER(:t))
          AND home_shots_target IS NOT NULL AND away_shots_target IS NOT NULL
          AND date < :c
        ORDER BY date DESC LIMIT :w
    """), engine, params={"t": team, "c": str(cutoff)[:10], "w": window})
    if len(df) < 3:
        return None
    xf, xa_, wts = [], [], []
    for i, r in df.iterrows():
        w = decay ** i
        home = r["home_team"].lower() == team.lower()
        sot = r["home_shots_target"] if home else r["away_shots_target"]
        sh = r["home_shots"] if home else r["away_shots"]
        sot_a = r["away_shots_target"] if home else r["home_shots_target"]
        sh_a = r["away_shots"] if home else r["home_shots"]
        xf.append((sot * 0.28 + max(sh - sot, 0) * 0.03) * w)
        xa_.append((sot_a * 0.28 + max(sh_a - sot_a, 0) * 0.03) * w)
        wts.append(w)
    return {"xg_for": sum(xf) / sum(wts), "xg_against": sum(xa_) / sum(wts)}


GLOBAL_CAL = 0.85   # mismo que aplica el pipeline real
_FACTOR_CACHE = None

def _market_factor(mkt):
    global _FACTOR_CACHE
    if _FACTOR_CACHE is None:
        try:
            import json
            from pathlib import Path
            f = json.loads((Path(__file__).parent.parent / "config" / "calibration_factors.json").read_text(encoding="utf-8"))
            _FACTOR_CACHE = f
        except Exception:
            _FACTOR_CACHE = {}
    node = _FACTOR_CACHE.get(mkt)
    if isinstance(node, dict) and node.get("n_bets", 0) >= 10:
        return float(node.get("factor", 1.0))
    return 1.0


def market_prob(mkt, lh, lw, league, calibrate=True):
    if mkt in ("over25", "under25"):
        r = totals_and_btts(lh, lw)
        p = r["over25"] if mkt == "over25" else r["under25"]
        try:
            p = p * 0.8 + get_over25_rate(league) * 0.2
        except Exception:
            pass
    else:
        ph, pdw, pa = match_outcomes(lh, lw)
        if mkt == "home_win":
            p = ph
        elif mkt == "away_win":
            p = pa
        elif mkt == "dc_1x":
            p = ph + pdw
        elif mkt == "dc_x2":
            p = pdw + pa
        elif mkt == "dc_12":
            p = ph + pa
        elif mkt == "dnb_home":
            p = ph / max(ph + pa, 1e-9)
        elif mkt == "dnb_away":
            p = pa / max(ph + pa, 1e-9)
        else:
            return None
    if calibrate:
        # La capa que el pipeline real SÍ aplica (y que la reconstrucción
        # cruda omitía): ×0.85 global × factor por mercado
        p = p * GLOBAL_CAL * _market_factor(mkt)
    return min(max(p, 0.03), 0.95)


def run(limit=400, min_edge=0.05, verbose=True):
    bets = pd.read_sql(text("""
        SELECT match, market, odds, stake, probability, result, profit,
               match_date, COALESCE(league,'') AS league
        FROM bets_history
        WHERE result IN ('win','loss','push','half_win','half_loss')
          AND odds > 1
        ORDER BY match_date DESC LIMIT :lim
    """), engine, params={"lim": limit})

    old_profit = old_staked = 0.0
    old_w = old_n = 0
    new_profit = new_staked = 0.0
    new_w = new_n = 0
    old_preds, old_outs = [], []
    new_preds, new_outs = [], []
    skipped = 0

    t0 = time.time()
    for _, b in bets.iterrows():
        try:
            h, a = str(b["match"]).split(" vs ")
        except ValueError:
            continue
        md = b["match_date"]
        try:
            fh = get_team_form(h, cutoff_date=md)
            fa = get_team_form(a, cutoff_date=md)
        except Exception:
            skipped += 1
            continue
        if not fh or not fa:
            skipped += 1
            continue
        xh = xg_asof(h, md)
        xa = xg_asof(a, md)
        XG_W = 0.40
        h_att, h_def = fh["attack_rating"], fh["defense_rating"]
        a_att, a_def = fa["attack_rating"], fa["defense_rating"]
        if xh:
            h_att = h_att * (1 - XG_W) + xh["xg_for"] * XG_W
            h_def = h_def * (1 - XG_W) + xh["xg_against"] * XG_W
        if xa:
            a_att = a_att * (1 - XG_W) + xa["xg_for"] * XG_W
            a_def = a_def * (1 - XG_W) + xa["xg_against"] * XG_W
        try:
            HA, TEMPO = get_lambda_multipliers(b["league"])
        except Exception:
            HA, TEMPO = 1.2, 1.1
        lh = min(h_att * a_def * HA * TEMPO, 2.5)
        lw = min(a_att * h_def * TEMPO, 2.5)
        try:
            h2h = get_h2h_stats(h, a)
            if h2h:
                lh = lh * 0.85 + h2h["h2h_home_goals"] * 0.15
                lw = lw * 0.85 + h2h["h2h_away_goals"] * 0.15
        except Exception:
            pass
        try:
            p_new = market_prob(b["market"], lh, lw, b["league"], calibrate=True)
        except Exception:
            p_new = None
        if p_new is None:
            skipped += 1
            continue
        p_new = min(max(p_new, 0.03), 0.95)

        # ── Modelo viejo: lo que realmente ocurrió ──
        pf = PROFIT_FACTOR.get(b["result"], 0.0)
        old_profit += b["profit"]
        old_staked += b["stake"]
        old_n += 1
        if b["result"] in ("win", "half_win"):
            old_w += 1
        old_preds.append(b["probability"])
        old_outs.append(1.0 if b["result"] in ("win", "half_win") else 0.0)

        # ── Modelo nuevo: simulación ──
        edge = p_new - 1.0 / b["odds"]
        if edge >= min_edge:
            new_n += 1
            new_staked += b["stake"]
            new_profit += pf * b["stake"] * (b["odds"] - 1)
            if b["result"] in ("win", "half_win"):
                new_w += 1
            new_preds.append(p_new)
            new_outs.append(1.0 if b["result"] in ("win", "half_win") else 0.0)

    print(f"\n{'='*66}")
    print(f"VALIDACIÓN RETROSPECTIVA — {old_n} bets analizadas "
          f"({skipped} sin datos suficientes) · {time.time()-t0:.0f}s")
    print(f"{'='*66}")
    print(f"\nMODELO VIEJO (real, las {old_n} bets):")
    print(f"  Apostado {old_staked:.1f}u | Profit {old_profit:+.2f}u | "
          f"ROI {old_profit/old_staked:+.1%}" if old_staked else "  sin datos")
    if old_preds:
        print(f"  WR {old_w/old_n:.1%} | predicho {sum(old_preds)/len(old_preds):.1%} | "
              f"brecha {sum(old_preds)/len(old_preds)-sum(old_outs)/len(old_outs):+.1%}")

    print(f"\nMODELO NUEVO (simulado, habría apostado {new_n}):")
    if new_n:
        print(f"  Apostado {new_staked:.1f}u | Profit {new_profit:+.2f}u | "
              f"ROI {new_profit/new_staked:+.1%}")
        print(f"  WR {new_w/new_n:.1%} | predicho {sum(new_preds)/len(new_preds):.1%} | "
              f"brecha {sum(new_preds)/len(new_preds)-sum(new_outs)/len(new_outs):+.1%}")
        print(f"  Filtró {old_n-new_n} de {old_n} bets viejas "
              f"({(old_n-new_n)/old_n:.0%})")
    else:
        print("  no habría apostado ninguna")
    print(f"\n  Nota: muestra retrospectiva SIN MLE/clima/motivación y con H2H")
    print(f"  sin corte temporal — es aproximación, no backtest perfecto.")
    return {"old_n": old_n, "new_n": new_n}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--edge", type=float, default=0.05)
    args = parser.parse_args()
    run(limit=args.limit, min_edge=args.edge)
