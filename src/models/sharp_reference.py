"""
src/models/sharp_reference.py
=============================
¿Tiene ventaja real el modelo? Medido contra Pinnacle (24-sep-26).

SOLO MIDE: no toca probabilidades, filtros ni stakes. Los precios salen de
src/features/pinnacle.py (misma descarga, sin créditos extra).

Tres mediciones sobre la cohorte actual (settings.LEARNING_SINCE), solo con
cierres de Pinnacle VÁLIDOS (descargados poco antes del kickoff y después
de abrir la candidata — la misma regla de closing_quality):

  1. Valor contra el cierre de Pinnacle: p_pinnacle_cierre × cuota − 1 por
     unidad, para las apuestas reales y para las candidatas shadow. Es la
     prueba estándar de ventaja y NO espera al resultado del partido.
     En empate anulado es el valor condicionado a que no haya empate
     (mismo signo que el valor total).
  2. ¿Pinnacle se mueve hacia el modelo? Cuando el modelo difiere de
     Pinnacle al abrir, cuántos puntos se acerca Pinnacle a él al cierre.
     Positivo = el modelo anticipa al mercado más preciso.
  3. Modelo vs Pinnacle en resultados: Brier(p_modelo) − Brier(p_pinnacle
     al cierre). Negativo = el modelo acierta más que el cierre de Pinnacle.

Los veredictos usan el mismo criterio que rule_evidence: IC 95% robusto por
partido y nada con menos de MIN_N filas o MIN_CLUSTERS partidos.
"""

from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sqlalchemy import text

from config.settings import LEARNING_SINCE
from src.models.rule_evidence import MIN_CLUSTERS, MIN_N, Z95, cluster_mean, verdict
from src.utils.closing_quality import CLOSING_MAX_LAG_MIN, CLOSING_MAX_LEAD_MIN
from src.utils.model_state import save_state

STATE_KEY = "sharp_reference"

_COLS = ["match", "match_date", "created_at", "market", "p_final", "odds",
         "edge_market", "pin_prob", "pin_close_prob", "pin_close_at", "result"]


# ─────────────────────────────────────────────────────────────────────────────
# Estadística (funciones puras)
# ─────────────────────────────────────────────────────────────────────────────

def _stat(values, clusters, lower_supports: bool) -> dict:
    s = cluster_mean(values, clusters)
    s["verdict"] = verdict(s, lower_supports, min_n=MIN_N, min_clusters=MIN_CLUSTERS)
    return {k: (round(float(v), 6) if isinstance(v, float) and np.isfinite(v)
                else (None if isinstance(v, float) else v))
            for k, v in s.items()}


def valid_pin_close(df: pd.DataFrame) -> pd.Series:
    """Cierre de Pinnacle descargado entre 150 min antes y 2 después del
    kickoff, y después de abrir la fila (closing_quality, vectorizado)."""
    at = pd.to_datetime(df["pin_close_at"], utc=True, errors="coerce")
    ko = pd.to_datetime(df["match_date"], utc=True, errors="coerce")
    opened = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    ok = (at.notna() & ko.notna()
          & (at >= ko - pd.Timedelta(minutes=CLOSING_MAX_LEAD_MIN))
          & (at <= ko + pd.Timedelta(minutes=CLOSING_MAX_LAG_MIN))
          & (opened.isna() | (at > opened)))
    return ok.fillna(False).astype(bool)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    d = (df.reset_index(drop=True) if len(df) else pd.DataFrame(columns=_COLS))
    for c in _COLS:
        if c not in d.columns:
            d[c] = None
    for c in ("p_final", "odds", "edge_market", "pin_prob", "pin_close_prob"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d["cluster"] = d["match"].astype(str) + "|" + d["match_date"].astype(str)
    d["valid_close"] = valid_pin_close(d) if len(d) else pd.Series(dtype=bool)
    return d


def value_at_close(d: pd.DataFrame) -> dict:
    """Valor esperado por unidad contra el cierre sin margen de Pinnacle."""
    m = d["valid_close"] & d["pin_close_prob"].notna() & (d["odds"] > 1)
    ev = d.loc[m, "pin_close_prob"] * d.loc[m, "odds"] - 1.0
    return _stat(ev, d.loc[m, "cluster"], lower_supports=False)


def move_toward_model(d: pd.DataFrame) -> dict:
    """Movimiento de Pinnacle (apertura → cierre) en la dirección del modelo."""
    m = (d["valid_close"] & d["pin_prob"].notna() & d["pin_close_prob"].notna()
         & d["p_final"].notna())
    dev = d.loc[m, "p_final"] - d.loc[m, "pin_prob"]
    move = d.loc[m, "pin_close_prob"] - d.loc[m, "pin_prob"]
    keep = dev.abs() > 1e-9
    return _stat(np.sign(dev[keep]) * move[keep], d.loc[m, "cluster"][keep], lower_supports=False)


def model_vs_pinnacle(d: pd.DataFrame) -> dict:
    """ΔBrier = Brier(modelo) − Brier(cierre de Pinnacle). Negativo = el modelo gana."""
    m = (d["valid_close"] & d["pin_close_prob"].notna() & d["p_final"].notna()
         & d["result"].isin(("win", "loss")))
    y = (d.loc[m, "result"] == "win").astype(float)
    diff = (d.loc[m, "p_final"] - y) ** 2 - (d.loc[m, "pin_close_prob"] - y) ** 2
    return _stat(diff, d.loc[m, "cluster"], lower_supports=True)


def build_sharp_report(shadow: pd.DataFrame, bets: pd.DataFrame,
                       min_edge: float | None = None) -> dict:
    if min_edge is None:
        from src.models.rule_evidence import rule_params
        min_edge = rule_params()["min_edge"]
    s, b = prepare(shadow), prepare(bets)
    bettable = s[s["edge_market"] >= min_edge]
    return {
        "since": LEARNING_SINCE,
        "min_edge": min_edge,
        "bets_value": value_at_close(b),
        "shadow_value": value_at_close(s),
        "bettable_value": value_at_close(bettable),
        "move_toward_model": move_toward_model(s),
        "model_vs_pinnacle": model_vs_pinnacle(s),
        "coverage": {
            "shadow": int(len(s)),
            "shadow_with_pin": int(s["pin_prob"].notna().sum()),
            "shadow_valid_close": int(s["valid_close"].sum()),
            "bets": int(len(b)),
            "bets_valid_close": int(b["valid_close"].sum()),
        },
        "policy": {"min_n": MIN_N, "min_clusters": MIN_CLUSTERS, "z": Z95},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────────────────
# DB y reporte
# ─────────────────────────────────────────────────────────────────────────────

def read_rows() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Solo lectura (salvo el ALTER idempotente de las columnas nuevas)."""
    from config.database import engine
    from src.models.save_bets import BETS_CLOSING_ALTER_SQL, SHADOW_ALTER_SQL, SHADOW_TABLE_SQL
    with engine.begin() as conn:
        conn.execute(text(SHADOW_TABLE_SQL))
        conn.execute(text(SHADOW_ALTER_SQL))
        conn.execute(text(BETS_CLOSING_ALTER_SQL))
    params = {"since": LEARNING_SINCE}
    shadow = pd.read_sql(text("""
        SELECT match, match_date, created_at, market, p_final, odds, edge_market,
               pin_prob, pin_close_prob, pin_close_at, result
        FROM shadow_bets
        WHERE match_date >= CAST(:since AS timestamptz) AND odds > 1.01
    """), engine, params=params)
    # match_date de bets_history es TIMESTAMP sin zona (UTC). 'stale' =
    # canceladas antes del kickoff o sin fuente: no son apuestas tomadas.
    bets = pd.read_sql(text("""
        SELECT match, match_date, created_at, market, probability AS p_final, odds,
               edge AS edge_market, NULL AS pin_prob, pin_close_prob, pin_close_at, result
        FROM bets_history
        WHERE match_date >= CAST(:since AS timestamp)
          AND result IS DISTINCT FROM 'stale'
    """), engine, params=params)
    return shadow, bets


def run_sharp_reference(verbose: bool = True) -> dict:
    """Paso semanal: arma el reporte, lo guarda en model_state y lo imprime."""
    shadow, bets = read_rows()
    report = build_sharp_report(shadow, bets)
    save_state(STATE_KEY, report)
    if verbose:
        print(format_report(report))
    return report


def has_data(report: dict) -> bool:
    cov = report.get("coverage", {})
    return bool(cov.get("shadow_valid_close") or cov.get("bets_valid_close"))


def _fmt(stat: dict, scale: float = 100.0, unit: str = "%", digits: int = 1) -> str:
    if stat.get("mean") is None:
        return "—"
    ci = f" ± {Z95 * stat['se'] * scale:.{digits}f}{unit}" if stat.get("se") is not None else ""
    return f"{stat['mean'] * scale:+.{digits}f}{unit}{ci}"


def format_report(rep: dict, html: bool = False) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    i = (lambda s: f"<i>{s}</i>") if html else (lambda s: s)
    cov = rep.get("coverage", {})
    lines = [b("📌 REFERENCIA PINNACLE (la casa más precisa)"),
             f"Desde {rep.get('since')} · solo mide, no cambia tus picks."]
    if not has_data(rep):
        lines.append("Aún no hay cierres de Pinnacle válidos.")
        return "\n".join(lines)

    def line(label, st, **kw):
        return f"{label}: {_fmt(st, **kw)}  (n={st.get('n', 0)}) → {st.get('verdict')}"

    lines += [
        line("Tus apuestas vs su cierre", rep["bets_value"]),
        line("Candidatas apostables", rep["bettable_value"]),
        line("Todas las candidatas", rep["shadow_value"]),
        line("Pinnacle se mueve hacia el modelo", rep["move_toward_model"], unit="pt"),
        line("Modelo vs Pinnacle (Brier, − = modelo mejor)", rep["model_vs_pinnacle"],
             scale=1.0, unit="", digits=4),
        f"Cobertura: {cov.get('shadow_with_pin', 0)}/{cov.get('shadow', 0)} candidatas con "
        f"precio de Pinnacle; cierres válidos: {cov.get('shadow_valid_close', 0)} candidatas, "
        f"{cov.get('bets_valid_close', 0)} apuestas.",
        i("Valor positivo con veredicto 'a favor' = ventaja real frente al mercado más "
          "preciso. 'insuficiente' = faltan datos (100 filas en 30 partidos). Las "
          "apuestas se miden a la mejor cuota europea; en PlayDoit suele ser menor."),
    ]
    return "\n".join(lines)
