"""
src/models/proposals.py
=======================
Lista exacta de lo que el modelo propuso cada día y qué pasó con cada una
(12-oct-26, a pedido del dueño).

Hasta aquí solo quedaban los conteos en el log ("55 sin valor frente a
Pinnacle"): no había forma de ver QUÉ apuestas descartó el filtro ni cómo
habrían terminado. Ahora cada apuesta que llega a la selección final
(_stage_select_bets) queda en `model_proposals` con su decisión: 'apostada'
o el motivo del descarte, en palabras. Se liquida con la MISMA función que
las apuestas reales y stake 1, así que su `profit` es la ganancia por unidad
de haberla jugado.

Un partido se evalúa varias veces (evening del día anterior, morning del
día): queda la última decisión, y ya liquidada no se toca.
"""

import pandas as pd
from sqlalchemy import text

from config.database import engine
from src.utils.log import get_logger

log = get_logger(__name__)

APOSTADA = "apostada"

# Motivos de descarte, en el orden en que los aplica _stage_select_bets.
LIGA_BLOQUEADA = "liga bloqueada"
PARTIDO_RARO = "partido poco fiable"
MERCADO_DESACTIVADO = "mercado desactivado"
# Mercados que tuvieron momio inventado hasta el 21-oct-26 (constante, no de
# una casa): cuando otra regla los frenaba antes (liga bloqueada) quedaban
# guardados con OTRA decisión y su "si se hubieran jugado" contaba igual.
FAKE_ODDS_MARKETS = ("over_1.5", "under_1.5", "over_3.5", "under_3.5",
                     "shots_over_5.5", "shots_under_5.5")
MERCADO_BLOQUEADO = "mercado bloqueado"
HANDICAP_BLOQUEADO = "hándicap bloqueado"
SIN_PINNACLE = "sin precio de Pinnacle"
NO_GANA_PINNACLE = "no le gana a Pinnacle por 2 pt"
HORARIO_SIN_VENTAJA = "a esta hora la ventaja no aguanta al cierre"
CUOTA_FUERA_DE_RANGO = "cuota fuera del rango del mercado"
VENTAJA_CORTA = "ventaja menor al mínimo"
DESVIO = "el modelo se aleja demasiado del mercado"
CUOTA_ALTA = "cuota demasiado alta"
EXCLUYENTE = "choca con otra apuesta del partido"
TOPE_PARTIDO = "tope de apuestas por partido"
LIMITE_CARTERA = "límite de cartera del día"
PAPEL = "solo papel"

PROPOSALS_DDL = """
    CREATE TABLE IF NOT EXISTS model_proposals (
        id           SERIAL PRIMARY KEY,
        match        TEXT NOT NULL,
        match_date   TIMESTAMPTZ NOT NULL,
        league       TEXT,
        market       TEXT NOT NULL,
        odds         DOUBLE PRECISION,
        probability  DOUBLE PRECISION,
        edge         DOUBLE PRECISION,
        pin_prob     DOUBLE PRECISION,
        decision     TEXT NOT NULL,
        stake        DOUBLE PRECISION,
        evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        result       TEXT,
        profit       DOUBLE PRECISION,
        resolved_at  TIMESTAMPTZ,
        UNIQUE (match, market, match_date)
    )
"""

_COLS = ("match", "match_date", "league", "market", "odds", "probability",
         "edge", "pin_prob", "decision", "stake")


def proposal(bet: dict, match: str, match_date, league, decision: str,
             pin_prob=None, stake=None) -> dict:
    """Registro de una apuesta propuesta (función pura)."""
    return {"match": match, "match_date": match_date, "league": league,
            "market": bet["market"], "odds": bet.get("odds"),
            "probability": bet.get("probability"),
            "edge": bet.get("edge_market", bet.get("edge")),
            "pin_prob": pin_prob, "decision": decision, "stake": stake}


def final_decisions(proposals: list, saved: list, paper: list) -> list:
    """
    Decisión final de cada propuesta (función pura). Las que la selección
    aceptó llegan como 'apostada' provisional; si después las quitó el
    límite de cartera o el estado aprendido, quedan con ese motivo. Las de
    ligas de papel, 'solo papel'.
    """
    key = lambda b: (b["match"], b["market"])
    saved_keys = {key(b) for b in saved}
    paper_keys = {key(b) for b in paper}
    out = []
    for p in proposals:
        p = dict(p)
        if p["decision"] == APOSTADA:
            if key(p) in paper_keys:
                p["decision"] = PAPEL
            elif key(p) not in saved_keys:
                p["decision"] = LIMITE_CARTERA
        out.append(p)
    return out


def _clean(v):
    if v is None:
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.to_pydatetime()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, "item") and not isinstance(v, (str, bytes)) else v


def persist_proposals(records: list, chunk: int = 200) -> int:
    """
    Guarda la última decisión de cada propuesta (upsert por partido, mercado
    y fecha). Lotes multi-VALUES: un viaje a Neon por lote, no por fila. Un
    fallo es ERROR (llega a Telegram): sin esta tabla no se puede revisar lo
    que el filtro descartó.
    """
    rows = [{c: _clean(r.get(c)) for c in _COLS} for r in records
            if r.get("odds") and r["odds"] > 1.01 and r.get("match_date") is not None]
    # la misma clave dos veces en un lote rompe el ON CONFLICT: queda la última
    rows = list({(r["match"], r["market"], str(r["match_date"])): r for r in rows}.values())
    if not rows:
        return 0
    try:
        with engine.begin() as conn:
            conn.execute(text(PROPOSALS_DDL))
            for i in range(0, len(rows), chunk):
                part = rows[i:i + chunk]
                values, params = [], {}
                for k, r in enumerate(part):
                    values.append("(" + ", ".join(f":{c}{k}" for c in _COLS) + ")")
                    params.update({f"{c}{k}": r[c] for c in _COLS})
                conn.execute(text(f"""
                    INSERT INTO model_proposals ({", ".join(_COLS)})
                    VALUES {", ".join(values)}
                    ON CONFLICT (match, market, match_date) DO UPDATE SET
                        league = EXCLUDED.league, odds = EXCLUDED.odds,
                        probability = EXCLUDED.probability, edge = EXCLUDED.edge,
                        pin_prob = EXCLUDED.pin_prob, decision = EXCLUDED.decision,
                        stake = EXCLUDED.stake, evaluated_at = NOW()
                    WHERE model_proposals.result IS NULL
                """), params)
    except Exception as e:
        log.error(f"❌ Propuestas del modelo no guardadas: {type(e).__name__}: {str(e)[:200]}")
        return 0
    print(f"📋 Propuestas del modelo: {len(rows)} guardadas "
          f"({sum(1 for r in rows if r['decision'] == APOSTADA)} apostadas)")
    return len(rows)


def resolve_proposals(dry_run: bool = False) -> dict:
    """Liquida las propuestas ya jugadas, igual que las candidatas shadow
    (misma función que las apuestas reales, stake 1). Sin datos a los
    SHADOW_STALE_DAYS días quedan 'stale'."""
    from src.models.save_bets import SHADOW_STALE_DAYS, _preload_matches, plan_shadow_resolutions
    with engine.begin() as conn:
        conn.execute(text(PROPOSALS_DDL))
    pending = pd.read_sql(text("""
        SELECT id, match, match_date, market, odds FROM model_proposals
        WHERE result IS NULL AND match_date < NOW() - INTERVAL '3 hours'
    """), engine)
    plan = plan_shadow_resolutions(pending, _preload_matches(pending["match_date"])) \
        if not pending.empty else []
    if dry_run:
        return {"pending": int(len(pending)), "resolved": len(plan), "dry_run": True}
    with engine.begin() as conn:
        if plan:
            conn.execute(text("""
                UPDATE model_proposals SET result = :result, profit = :profit, resolved_at = NOW()
                WHERE id = :id AND result IS NULL
            """), plan)
        stale = conn.execute(text(f"""
            UPDATE model_proposals SET result = 'stale', resolved_at = NOW()
            WHERE result IS NULL AND match_date < NOW() - INTERVAL '{SHADOW_STALE_DAYS} days'
        """)).rowcount
    print(f"📋 Propuestas liquidadas: {len(plan)}/{len(pending)}"
          + (f", {stale} sin datos → stale" if stale else ""))
    return {"pending": int(len(pending)), "resolved": len(plan), "stale": int(stale or 0)}


PLAYED = ("win", "loss", "push", "half_win", "half_loss")


def summarize(df: pd.DataFrame) -> dict:
    """Resumen de propuestas liquidadas (función pura): total, apostadas y,
    si se hubieran jugado todas a 1u, ganadas/perdidas y profit."""
    played = df[df["result"].isin(PLAYED)]
    def wl(d):
        return (int(d["result"].isin(["win", "half_win"]).sum()),
                int(d["result"].isin(["loss", "half_loss"]).sum()),
                round(float(d["profit"].sum()), 2))
    w, l, u = wl(played)
    bet = played[played["decision"] == APOSTADA]
    bw, bl, bu = wl(bet)
    by_reason = (played.groupby("decision")["profit"].agg(["count", "sum"])
                 .sort_values("count", ascending=False))
    return {"total": int(len(df)), "played": int(len(played)), "wins": w, "losses": l,
            "profit": u, "bet": int(len(bet)), "bet_wins": bw, "bet_losses": bl,
            "bet_profit": bu,
            "by_reason": [(r, int(c), round(float(s), 2)) for r, (c, s) in by_reason.iterrows()]}


def weekly_lines(days: int = 7) -> list:
    """Bloque del reporte semanal de Telegram: lo que el modelo propuso y
    cómo habría terminado. Vacío si la tabla todavía no tiene datos."""
    try:
        with engine.connect() as conn:
            # La tabla nace con la primera corrida del pipeline que guarda
            # propuestas: el weekly del 12-oct-26 corrió antes y avisó ERROR.
            if conn.execute(text("SELECT to_regclass('model_proposals')")).scalar() is None:
                return []
        # Sin los mercados desactivados (19-oct-26): su cuota es una constante
        # inventada (más/menos 1.5 y 3.5 goles, tiros), no la de una casa, y
        # su "si se hubieran jugado" sumaba ganancias que nadie podía tomar.
        df = pd.read_sql(text("""
            SELECT decision, result, profit FROM model_proposals
            WHERE match_date >= NOW() - make_interval(days => :d) AND match_date < NOW()
              AND decision <> :disabled AND NOT (market = ANY(:fake))
        """), engine, params={"d": days, "disabled": MERCADO_DESACTIVADO, "fake": list(FAKE_ODDS_MARKETS)})
    except Exception as e:
        log.error(f"❌ Resumen de propuestas: {type(e).__name__}: {str(e)[:160]}")
        return []
    if df.empty or not {"decision", "result", "profit"} <= set(df.columns):
        return []
    s = summarize(df)
    sign = lambda x: f"{'+' if x >= 0 else ''}{x:.2f}u"
    lines = ["— <b>LO QUE EL MODELO PROPUSO</b> —",
             f"Propuestas: {s['total']}  ·  apostadas: {s['bet']}",
             f"Si se hubieran jugado todas (1u c/u): {s['wins']} ganadas, "
             f"{s['losses']} perdidas, {sign(s['profit'])}"]
    for reason, n, u in s["by_reason"][:4]:
        lines.append(f"  {reason}: {n} → {sign(u)}")
    lines.append("")
    return lines
