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
(primera vez que se vio); en PlayDoit suele ser menor.
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


def build_lab(shadow: pd.DataFrame) -> dict:
    """Función pura: cada estrategia con su valor contra el cierre,
    veredicto y resultados a 1u."""
    profit = shadow["profit"] if "profit" in shadow.columns else None
    d = prepare(shadow)
    d["profit"] = profit.reset_index(drop=True) if profit is not None else None
    out = {}
    for key, name, rule in STRATEGIES:
        sel = d[rule(d).fillna(False).astype(bool)] if len(d) else d
        out[key] = {"name": name, "candidates": int(len(sel)),
                    "value": value_at_close(sel), "results": results(sel)}
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
    lines.append("Cuando una salga ✅ con 100 o más, se puede pasar a dinero real (decisión tuya).")
    return lines


def read_shadow() -> pd.DataFrame:
    from config.database import engine
    from config.settings import LEARNING_SINCE
    from sqlalchemy import text
    return pd.read_sql(text("""
        SELECT match, match_date, created_at, market, p_final, odds, edge_market,
               pin_prob, pin_close_prob, pin_close_at, result, profit
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
