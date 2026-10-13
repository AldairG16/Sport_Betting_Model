"""
src/models/strategy_lab.py
==========================
Laboratorio de estrategias: varias formas de elegir apuestas, medidas a la
vez y SIN dinero sobre las candidatas shadow (13-oct-26, a pedido del dueño:
"solo mejoras que aporten sin gastar dinero").

Cada estrategia es un filtro sobre las candidatas que el sistema ya registra
todos los días. Se juzga contra el cierre sin margen de Pinnacle, con la
misma estadística que sharp_reference (cierres válidos, IC 95% robusto por
partido, veredicto con 100 filas en 30 partidos): ~100 candidatas bastan, en
vez de los miles de resultados que harían falta para ver la ganancia.

SOLO MIDE. Pasar una estrategia a dinero real es decisión del dueño, con el
veredicto en la mano.

Precios: la cuota de cada candidata es la MEJOR europea al registrarla
(primera vez que se vio). Desde el 13-oct-26 también se mide al precio
PROMEDIO entre casas (odds_avg: 1X2 y over/under 2.5), que es lo que paga
una casa típica como PlayDoit: la mejor de ~20 casas queda ~8% arriba del
promedio, y medir solo con ella hacía ver ventajas que el dueño no puede
tomar. Una estrategia solo es real si gana también al precio promedio.
"""

from datetime import datetime, timezone

import pandas as pd

from src.models.rule_evidence import (SETTLED, VERDICT_AGAINST, VERDICT_FOR,
                                      VERDICT_NONE, VERDICT_THIN)
from src.models.sharp_reference import prepare, value_at_close

STATE_KEY = "strategy_lab"


def _pin_adv(d):
    return d["pin_prob"] - 1.0 / d["odds"]


def _model_edge(d):
    return d["p_final"] - 1.0 / d["odds"]


# (clave, nombre para el dueño, filtro sobre las candidatas preparadas)
STRATEGIES = (
    ("modelo", "Lo que el modelo quiere (ve 2 pt o más)",
     lambda d: _model_edge(d) >= 0.02),
    ("actual", "Filtro actual: modelo + precio 2 pt sobre Pinnacle",
     lambda d: (_model_edge(d) >= 0.02) & (_pin_adv(d) >= 0.02)),
    ("precio_0", "Precio sobre Pinnacle, sin preguntar al modelo",
     lambda d: _pin_adv(d) >= 0.0),
    ("precio_1", "Precio 1 pt sobre Pinnacle, sin preguntar al modelo",
     lambda d: _pin_adv(d) >= 0.01),
    ("precio_y_modelo", "Precio sobre Pinnacle y el modelo de acuerdo",
     lambda d: (_pin_adv(d) >= 0.0) & (_model_edge(d) >= 0.02)),
)

VERDICT_TEXT = {
    VERDICT_FOR: "✅ le gana al mercado",
    VERDICT_AGAINST: "❌ pierde contra el mercado",
    VERDICT_NONE: "⚖️ sin diferencia clara todavía",
}


def results(d: pd.DataFrame) -> dict:
    """Si se hubieran jugado a 1u: jugadas, ganadas, perdidas y profit."""
    p = d[d["result"].isin(SETTLED)]
    profit = pd.to_numeric(p.get("profit"), errors="coerce").fillna(0.0)
    return {"played": int(len(p)),
            "wins": int(p["result"].isin(("win", "half_win")).sum()),
            "losses": int(p["result"].isin(("loss", "half_loss")).sum()),
            "profit": round(float(profit.sum()), 2)}


def value_at_average(sel: pd.DataFrame) -> dict:
    """El mismo valor contra el cierre, pero tomando la cuota PROMEDIO
    entre casas en vez de la mejor (solo candidatas que la tienen)."""
    avg = pd.to_numeric(sel.get("odds_avg"), errors="coerce") if len(sel) else pd.Series(dtype=float)
    m = avg.notna() & (avg > 1)
    return value_at_close(sel[m].assign(odds=avg[m]))


def build_lab(shadow: pd.DataFrame) -> dict:
    """Función pura: cada estrategia con su valor contra el cierre (a la
    mejor cuota y a la promedio), veredicto y resultados a 1u."""
    extra = {c: shadow[c].reset_index(drop=True) for c in ("profit", "odds_avg")
             if c in shadow.columns}
    d = prepare(shadow)
    for c in ("profit", "odds_avg"):
        d[c] = extra.get(c)
    out = {}
    for key, name, rule in STRATEGIES:
        sel = d[rule(d).fillna(False).astype(bool)] if len(d) else d
        out[key] = {"name": name, "candidates": int(len(sel)),
                    "value": value_at_close(sel), "value_avg": value_at_average(sel),
                    "results": results(sel)}
    return {"strategies": out, "updated_at": datetime.now(timezone.utc).isoformat()}


def format_lines(lab: dict, html: bool = True) -> list:
    """Bloque para Telegram, en palabras del dueño."""
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    lines = ["", b("🧪 LABORATORIO DE ESTRATEGIAS (sin dinero)"),
             "Contra el cierre de Pinnacle: + = tomamos mejor precio que el justo."]
    for key, _, _ in STRATEGIES:
        st = lab["strategies"].get(key)
        if not st:
            continue
        v, r = st["value"], st["results"]
        if v.get("verdict") == VERDICT_THIN or v.get("mean") is None:
            verdict = f"⏳ faltan datos ({v.get('n', 0)} de 100)"
            value = ""
        else:
            verdict = VERDICT_TEXT.get(v["verdict"], v["verdict"])
            value = f"{v['mean'] * 100:+.1f}% (±{1.96 * v['se'] * 100:.1f}) · "
        res = (f"a 1u: {r['wins']}G/{r['losses']}P {r['profit']:+.2f}u"
               if r["played"] else "sin resultados aún")
        lines.append(f"• {st['name']}: {value}{verdict} · {res}")
        va = st.get("value_avg") or {}
        if va.get("mean") is not None and va.get("verdict") != VERDICT_THIN:
            lines.append(f"   a precio promedio (tu casa): {va['mean'] * 100:+.1f}% → "
                         f"{VERDICT_TEXT.get(va['verdict'], va['verdict'])}")
        else:
            lines.append(f"   a precio promedio (tu casa): ⏳ midiendo ({va.get('n', 0)} de 100)")
    lines.append("Cuenta la de precio promedio: es lo que paga una casa como PlayDoit. "
                 "Cuando una salga ✅ ahí con 100 o más, se puede pasar a dinero real (decisión tuya).")
    return lines


def read_shadow() -> pd.DataFrame:
    from config.database import engine
    from config.settings import LEARNING_SINCE
    from sqlalchemy import text
    from src.models.save_bets import SHADOW_ALTER_SQL
    with engine.begin() as conn:
        conn.execute(text(SHADOW_ALTER_SQL))     # odds_avg existe antes de la 1ª corrida nueva
    return pd.read_sql(text("""
        SELECT match, match_date, created_at, market, p_final, odds, edge_market,
               pin_prob, pin_close_prob, pin_close_at, result, profit, odds_avg
        FROM shadow_bets
        WHERE match_date >= CAST(:since AS timestamptz) AND odds > 1.01
    """), engine, params={"since": LEARNING_SINCE})


def run_strategy_lab(verbose: bool = True) -> dict:
    """Paso semanal: mide, guarda en model_state (con historia) y devuelve."""
    from src.utils.model_state import save_state
    lab = build_lab(read_shadow())
    save_state(STATE_KEY, lab)
    if verbose:
        print("\n".join(format_lines(lab, html=False)))
    return lab
