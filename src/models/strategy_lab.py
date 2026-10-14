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


# Mercados que Pinnacle no publica en el feed y Kalshi sí (14-oct-26):
# se miden contra el cierre de Kalshi, con la misma regla de cierre válido.
KALSHI_MARKETS = ("btts", "btts_no", "h1_home", "h1_draw", "h1_away")
KALSHI_STRATEGIES = (
    ("k_modelo", "Sin Pinnacle (ambos anotan, 1er tiempo): lo que quiere el modelo",
     lambda d: _model_edge(d) >= 0.02),
    ("k_precio", "Sin Pinnacle: precio sobre Kalshi, sin preguntar al modelo",
     lambda d: _pin_adv(d) >= 0.0),
)


def with_kalshi(shadow: pd.DataFrame, kh: pd.DataFrame) -> pd.DataFrame:
    """
    Candidatas sin precio de Pinnacle en mercados que Kalshi cubre, con la
    referencia de Kalshi en las columnas de Pinnacle (función pura): pin_prob
    = la de Kalshi al registrarse la candidata (la última antes, o la primera
    después si aún no había foto) y pin_close_prob / pin_close_at = la última
    foto antes del kickoff. valid_pin_close decide si ese cierre vale.
    """
    cols = list(shadow.columns)
    if shadow.empty or kh is None or kh.empty:
        return shadow.iloc[0:0]
    s = shadow[shadow["pin_prob"].isna() & shadow["market"].isin(KALSHI_MARKETS)].copy()
    if s.empty:
        return s
    teams = s["match"].astype(str).str.split(" vs ", n=1, expand=True)
    s["home"], s["away"] = teams[0], teams[1]
    s["ko"] = pd.to_datetime(s["match_date"], utc=True)
    k = kh.copy()
    k["ko"] = pd.to_datetime(k["kickoff"], utc=True)
    k["at"] = pd.to_datetime(k["captured_at"], utc=True)
    k["prob"] = pd.to_numeric(k["prob"], errors="coerce")
    m = s.reset_index().merge(k[["home", "away", "ko", "market", "at", "prob"]],
                              on=["home", "away", "ko", "market"], how="inner")
    if m.empty:
        return s.iloc[0:0][cols]
    created = pd.to_datetime(m["created_at"], utc=True)
    out = []
    for idx, g in m.assign(created=created).groupby("index"):
        g = g.sort_values("at")
        before = g[g["at"] <= g["created"]]
        open_p = (before["prob"].iloc[-1] if len(before) else g["prob"].iloc[0])
        pre = g[g["at"] <= g["ko"]]
        row = s.loc[idx, cols].copy()
        row["pin_prob"] = open_p
        if len(pre):
            row["pin_close_prob"] = pre["prob"].iloc[-1]
            row["pin_close_at"] = pre["at"].iloc[-1]
        else:
            row["pin_close_prob"], row["pin_close_at"] = None, None
        out.append(row)
    return pd.DataFrame(out, columns=cols)


def build_lab(shadow: pd.DataFrame, kalshi: pd.DataFrame | None = None) -> dict:
    """Función pura: cada estrategia con su valor contra el cierre (a la
    mejor cuota y a la promedio), veredicto y resultados a 1u; y, con los
    precios de Kalshi, las de los mercados sin Pinnacle."""
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
    kal = {}
    ks = with_kalshi(shadow, kalshi)
    if len(ks):
        kprofit = ks["profit"].reset_index(drop=True) if "profit" in ks.columns else None
        kd = prepare(ks)
        kd["profit"] = kprofit
        for key, name, rule in KALSHI_STRATEGIES:
            sel = kd[rule(kd).fillna(False).astype(bool)]
            kal[key] = {"name": name, "candidates": int(len(sel)),
                        "value": value_at_close(sel), "results": results(sel)}
    return {"strategies": out, "kalshi": kal, "updated_at": datetime.now(timezone.utc).isoformat()}


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
    kal = lab.get("kalshi") or {}
    lines += ["", b("📈 Mercados sin Pinnacle, contra el cierre de Kalshi") + " (mejor cuota europea)"]
    if not kal:
        lines.append("⏳ juntando precios de Kalshi (empezó el 14-oct)")
    for key, _, _ in KALSHI_STRATEGIES:
        st = kal.get(key)
        if not st:
            continue
        v, r = st["value"], st["results"]
        if v.get("verdict") == VERDICT_THIN or v.get("mean") is None:
            verdict, value = f"⏳ faltan datos ({v.get('n', 0)} de 100)", ""
        else:
            verdict = VERDICT_TEXT.get(v["verdict"], v["verdict"])
            value = f"{v['mean'] * 100:+.1f}% (±{1.96 * v['se'] * 100:.1f}) · "
        res = (f"a 1u: {r['wins']}G/{r['losses']}P {r['profit']:+.2f}u"
               if r["played"] else "sin resultados aún")
        lines.append(f"• {st['name']}: {value}{verdict} · {res}")
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


def read_kalshi() -> pd.DataFrame:
    """Fotos de Kalshi (src/features/kalshi.py); vacío si aún no hay tabla."""
    from config.database import engine
    from config.settings import LEARNING_SINCE
    from sqlalchemy import text
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('kalshi_history')")).scalar() is None:
            return pd.DataFrame(columns=["home", "away", "kickoff", "market", "prob", "captured_at"])
    return pd.read_sql(text("""
        SELECT home, away, kickoff, market, prob, captured_at FROM kalshi_history
        WHERE kickoff >= CAST(:since AS timestamptz)
    """), engine, params={"since": LEARNING_SINCE})


def run_strategy_lab(verbose: bool = True) -> dict:
    """Paso semanal: mide, guarda en model_state (con historia) y devuelve."""
    from src.utils.model_state import save_state
    lab = build_lab(read_shadow(), read_kalshi())
    save_state(STATE_KEY, lab)
    if verbose:
        print("\n".join(format_lines(lab, html=False)))
    return lab
