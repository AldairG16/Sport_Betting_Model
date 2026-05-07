"""
src/models/drift_detector.py
============================
Detector de drift (Sprint 2 — Mejora #15).

Compara la distribución de residuales del modelo (prob - outcome) en una
ventana RECIENTE vs una ventana de REFERENCIA. Si la distribución cambió
significativamente (test KS), dispara alerta — el modelo está dejando de
funcionar para esa liga/mercado y conviene revisar el fit.

Triggers de alerta:
  • KS p-value < 0.05  → distribución cambió
  • |Δ Brier| > 0.05   → calibración degradada
  • |Δ ROI| > 10pp     → performance degradada

Uso:
  from src.models.drift_detector import detect_drift, format_drift_report
  report = detect_drift()
  print(format_drift_report(report))

Llamado por orchestrator en modo `weekly` y por `tests/test_drift.py`.
Read-only sobre `bets_history`.
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.append(str(Path(__file__).parent.parent.parent))


# Ventanas: reference vs current
_REF_DAYS_FROM = 120     # ventana referencia: hace 120-60 días (estable)
_REF_DAYS_TO   = 60
_CUR_DAYS      = 30      # ventana actual: últimos 30 días

_KS_PVAL_ALERT = 0.05    # p-value KS bajo este → drift detectado
_BRIER_ALERT   = 0.05    # |Δ Brier| sobre este → drift detectado
_ROI_ALERT     = 0.10    # |Δ ROI| sobre este → drift detectado
_MIN_N         = 15      # n mínimo en cada ventana para hacer test


def _ks_pvalue(a: np.ndarray, b: np.ndarray) -> float:
    """Kolmogorov-Smirnov 2-sample test. Retorna p-value."""
    from scipy.stats import ks_2samp
    if len(a) < 5 or len(b) < 5:
        return 1.0   # no hay datos para test → no hay drift
    try:
        return float(ks_2samp(a, b).pvalue)
    except Exception:
        return 1.0


def detect_drift(by_league: bool = True) -> dict:
    """
    Compara dos ventanas de bets resueltas y reporta drift.

    Args:
        by_league: si True, también desagrega por liga (más sensible).

    Returns:
        dict con:
          - "global": {ks_p, brier_ref, brier_cur, roi_ref, roi_cur, alert}
          - "by_market": {market: {...}}
          - "by_league": {(league, market): {...}}  si by_league=True
          - "alerts":   list[str]  resumen de problemas
    """
    from config.database import engine

    df = pd.read_sql(f"""
        SELECT match_date, league, market, probability, odds, result
        FROM bets_history
        WHERE result IN ('win','loss')
          AND probability BETWEEN 0.01 AND 0.99
          AND match_date >= NOW() - INTERVAL '{_REF_DAYS_FROM} days'
    """, engine)
    if df.empty:
        return {"alerts": ["sin datos para drift detection"]}

    df["outcome"]   = (df["result"] == "win").astype(int)
    df["residual"]  = df["probability"] - df["outcome"]
    df["pnl"]       = df.apply(
        lambda r: r["odds"] - 1 if r["result"] == "win" else -1, axis=1
    )
    df["match_date"] = pd.to_datetime(df["match_date"])
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)

    ref_lo = now - pd.Timedelta(days=_REF_DAYS_FROM)
    ref_hi = now - pd.Timedelta(days=_REF_DAYS_TO)
    cur_lo = now - pd.Timedelta(days=_CUR_DAYS)

    # Asegurar que match_date es naive para la comparación
    df["match_date"] = df["match_date"].dt.tz_localize(None) if df["match_date"].dt.tz is not None else df["match_date"]

    ref_mask = (df["match_date"] >= ref_lo) & (df["match_date"] < ref_hi)
    cur_mask = (df["match_date"] >= cur_lo)
    ref = df[ref_mask]
    cur = df[cur_mask]

    def _stats(d: pd.DataFrame) -> dict:
        if len(d) == 0:
            return {"n": 0, "brier": None, "roi": None, "residuals": np.array([])}
        return {
            "n":         int(len(d)),
            "brier":     float(np.mean(d["residual"] ** 2)),
            "roi":       float(d["pnl"].mean()),
            "residuals": d["residual"].values,
        }

    def _alert(ref_s: dict, cur_s: dict) -> dict:
        if ref_s["n"] < _MIN_N or cur_s["n"] < _MIN_N:
            return {"flag": False, "reason": f"sample chico (ref={ref_s['n']}, cur={cur_s['n']})"}

        ks_p = _ks_pvalue(ref_s["residuals"], cur_s["residuals"])
        d_brier = cur_s["brier"] - ref_s["brier"]
        d_roi   = cur_s["roi"]   - ref_s["roi"]

        flags = []
        if ks_p < _KS_PVAL_ALERT:        flags.append(f"KS p={ks_p:.3f}")
        if abs(d_brier) > _BRIER_ALERT:  flags.append(f"ΔBrier={d_brier:+.3f}")
        if abs(d_roi)   > _ROI_ALERT:    flags.append(f"ΔROI={d_roi*100:+.1f}pp")

        return {
            "flag":    bool(flags),
            "reasons": flags,
            "ks_p":    round(ks_p, 4),
            "ref": {k: ref_s[k] for k in ("n","brier","roi")},
            "cur": {k: cur_s[k] for k in ("n","brier","roi")},
            "delta_brier": round(d_brier, 4),
            "delta_roi":   round(d_roi, 4),
        }

    out: dict = {"alerts": []}
    out["global"] = _alert(_stats(ref), _stats(cur))
    if out["global"]["flag"]:
        out["alerts"].append(f"global: {', '.join(out['global']['reasons'])}")

    # Por mercado
    out["by_market"] = {}
    for mkt in df["market"].unique():
        ref_s = _stats(ref[ref["market"] == mkt])
        cur_s = _stats(cur[cur["market"] == mkt])
        a = _alert(ref_s, cur_s)
        out["by_market"][mkt] = a
        if a["flag"]:
            out["alerts"].append(f"market {mkt}: {', '.join(a['reasons'])}")

    # Por liga × mercado (si se pidió)
    if by_league:
        out["by_league_market"] = {}
        for (lg, mkt), _ in df.groupby(["league", "market"]):
            sub_ref = ref[(ref["league"] == lg) & (ref["market"] == mkt)]
            sub_cur = cur[(cur["league"] == lg) & (cur["market"] == mkt)]
            ref_s = _stats(sub_ref)
            cur_s = _stats(sub_cur)
            a = _alert(ref_s, cur_s)
            if a["flag"]:
                key = f"{lg}|{mkt}"
                out["by_league_market"][key] = a
                out["alerts"].append(f"{key}: {', '.join(a['reasons'])}")

    return out


def format_drift_report(report: dict, max_lines: int = 20) -> str:
    """Convierte el dict del detector en string legible para Telegram/console."""
    if not report.get("alerts"):
        return "✅ Sin drift significativo detectado"

    lines = ["🚨 <b>DRIFT DETECTADO</b>", ""]
    g = report.get("global", {})
    if g.get("flag"):
        ref = g.get("ref", {})
        cur = g.get("cur", {})
        lines.append(
            f"  GLOBAL: ref(n={ref.get('n',0)}) Brier {ref.get('brier',0):.3f} ROI {ref.get('roi',0)*100:+.1f}%"
        )
        lines.append(
            f"          cur(n={cur.get('n',0)}) Brier {cur.get('brier',0):.3f} ROI {cur.get('roi',0)*100:+.1f}%"
        )
        lines.append(f"          → {', '.join(g['reasons'])}")
        lines.append("")

    if report["alerts"]:
        lines.append("Por mercado/liga:")
        for a in report["alerts"][:max_lines]:
            if not a.startswith("global"):
                lines.append(f"  • {a}")

    if len(report["alerts"]) > max_lines:
        lines.append(f"  ... y {len(report['alerts']) - max_lines} más")

    return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rep = detect_drift()
    print(format_drift_report(rep))
