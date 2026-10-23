"""
src/models/playdoit_prices.py
=============================
Momios reales de PlayDoit que el dueño manda por captura (21-oct-26).

PlayDoit no está en ninguna API de momios y su página bloquea a los
programas automáticos. El dueño manda una captura al bot de Telegram
(scripts/playdoit_checker.py), Claude la lee y aquí se decide, momio por
momio, si vale la pena con la MISMA regla del sistema: la probabilidad más
baja entre el programa y el mercado sin margen (Pinnacle en el 1X2; el
promedio de las casas en goles y ambos anotan), menos 2 puntos
(src/utils/min_odds.py). Cada momio queda guardado en `playdoit_prices`:
con el tiempo se aprende cuánto paga PlayDoit frente al promedio de las
casas, y el dashboard lo usa para estimar el momio de PlayDoit.

Funciones puras salvo las de base de datos (al final).
"""

import difflib
import math
import re

from sqlalchemy import text

from src.utils.min_odds import MIN_EDGE_TO_PLACE, fmt_american, min_american, to_american

TABLE = "playdoit_prices"
MATCH_SIMILARITY = 0.72        # parecido mínimo de nombres (cada equipo) si no coincide exacto
RATIO_MIN_N = 10               # momios guardados para estimar PlayDoit / promedio

DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id SERIAL PRIMARY KEY,
    captured_at TIMESTAMPTZ DEFAULT NOW(),
    message_id BIGINT,
    match TEXT, kickoff TIMESTAMPTZ, league TEXT,
    market TEXT, line NUMERIC,
    american INT, decimal NUMERIC, boosted BOOLEAN,
    consensus_decimal NUMERIC, pin_fair NUMERIC, model_prob NUMERIC,
    min_american INT, verdict TEXT,
    raw JSONB
)
"""


# ─────────────────────────────────────────────────────────────────────────────
# Momios
# ─────────────────────────────────────────────────────────────────────────────

def parse_price(value) -> float | None:
    """Momio como lo muestra PlayDoit → decimal. "+150" → 2.5, "-120" →
    1.833, "2.50" → 2.5. None si no se entiende."""
    s = str(value or "").strip().replace(" ", "").replace(",", ".").replace("−", "-")
    if not s:
        return None
    m = re.fullmatch(r"([+-])(\d{3,5})", s)
    if m:
        a = int(m.group(2))
        if a < 100:
            return None
        return round(1 + a / 100, 4) if m.group(1) == "+" else round(1 + 100 / a, 4)
    m = re.fullmatch(r"\d{1,3}(\.\d+)?", s)
    if m:
        d = float(s)
        return d if d > 1.0 else None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Mercados: lo que lee Claude → la clave interna
# ─────────────────────────────────────────────────────────────────────────────

LABELS = {"home_win": "Gana {h}", "draw": "Empate", "away_win": "Gana {a}",
          "dc_1x": "{h} o empate", "dc_x2": "Empate o {a}", "dc_12": "{h} o {a}",
          "dnb_home": "Empate anula · {h}", "dnb_away": "Empate anula · {a}",
          "over25": "Más de 2.5 goles", "under25": "Menos de 2.5 goles",
          "btts": "Ambos anotan: sí", "btts_no": "Ambos anotan: no"}


def market_key(item: dict) -> str | None:
    """{"mercado", "seleccion", "linea"} de la captura → clave interna, o
    None si el sistema no tiene referencia para ese mercado."""
    mk = str(item.get("mercado") or "").lower()
    sel = str(item.get("seleccion") or "").lower().strip()
    line = item.get("linea")
    if mk == "1x2":
        return {"local": "home_win", "1": "home_win", "empate": "draw", "x": "draw",
                "visitante": "away_win", "2": "away_win"}.get(sel)
    if mk == "doble_oportunidad":
        return {"1x": "dc_1x", "x2": "dc_x2", "12": "dc_12"}.get(sel.replace(" ", ""))
    if mk == "empate_anula":
        return {"local": "dnb_home", "1": "dnb_home", "visitante": "dnb_away", "2": "dnb_away"}.get(sel)
    if mk == "total_goles":
        try:
            if abs(float(line) - 2.5) > 1e-9:
                return None
        except (TypeError, ValueError):
            return None
        return {"mas": "over25", "más": "over25", "over": "over25",
                "menos": "under25", "under": "under25"}.get(sel)
    if mk == "ambos_anotan":
        return {"si": "btts", "sí": "btts", "yes": "btts", "no": "btts_no"}.get(sel)
    return None


def _norm(name: str, normalize=None) -> str:
    n = normalize(name) if normalize else str(name)
    return str(n).lower().strip()


def find_fixture(home: str, away: str, fixtures: list, normalize=None) -> dict | None:
    """El partido del sistema que corresponde a lo que se lee en la captura.
    fixtures: [{"home_norm", "away_norm", "home", "away", ...}]."""
    h, a = _norm(home, normalize), _norm(away, normalize)
    for f in fixtures:
        if f["home_norm"] == h and f["away_norm"] == a:
            return f
    best, score = None, 0.0
    sim = lambda x, y: difflib.SequenceMatcher(None, x, y).ratio()
    for f in fixtures:
        sh = max(sim(h, f["home_norm"]), sim(h, str(f["home"]).lower()))
        sa = max(sim(a, f["away_norm"]), sim(a, str(f["away"]).lower()))
        if sh >= MATCH_SIMILARITY and sa >= MATCH_SIMILARITY and sh + sa > score:
            best, score = f, sh + sa
    return best


# ─────────────────────────────────────────────────────────────────────────────
# Probabilidades de referencia de un partido
# ─────────────────────────────────────────────────────────────────────────────

def _fair(*odds) -> list | None:
    try:
        o = [float(x) for x in odds]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(x) and x > 1 for x in o):
        return None
    inv = [1 / x for x in o]
    return [x / sum(inv) for x in inv]


def _three_way_derived(p: list) -> dict:
    h, d, a = p
    return {"home_win": h, "draw": d, "away_win": a, "dc_1x": h + d, "dc_x2": d + a, "dc_12": h + a,
            "dnb_home": h / (h + a) if h + a else None, "dnb_away": a / (h + a) if h + a else None}


def reference_probs(f: dict, bias: dict | None = None) -> tuple[dict, dict]:
    """(mercado sin margen, programa) por clave interna para un partido.
    f trae los momios de upcoming_matches y, si hay, el modelo
    (model_vs_sharp: p_home/p_draw/p_away/p_over25/p_btts, crudo). bias: el
    sesgo aprendido (side_bias); el programa sale corregido como en los picks
    (23-oct-26: aquí se usaba el crudo)."""
    from src.models.side_bias import program_probs
    market = {}
    pin = _fair(f.get("pin_home_odds"), f.get("pin_draw_odds"), f.get("pin_away_odds"))
    if pin:
        market.update(_three_way_derived(pin))
    ou = _fair(f.get("consensus_over25_odds"), f.get("consensus_under25_odds")) or \
        _fair(f.get("over25_odds"), f.get("under25_odds"))
    if ou:
        market.update({"over25": ou[0], "under25": ou[1]})
    bt = _fair(f.get("btts_yes_odds"), f.get("btts_no_odds"))
    if bt:
        market.update({"btts": bt[0], "btts_no": bt[1]})
    model = {}
    p = program_probs(f, bias)
    if None not in (p["home_win"], p["draw"], p["away_win"]):
        model.update(_three_way_derived([p["home_win"], p["draw"], p["away_win"]]))
    for k_yes, k_no in (("over25", "under25"), ("btts", "btts_no")):
        if p[k_yes] is not None:
            model.update({k_yes: p[k_yes], k_no: 1 - p[k_yes]})
    return market, model


def consensus_decimal(f: dict, key: str) -> float | None:
    """Momio promedio de las casas para ese mercado (para aprender cuánto
    paga PlayDoit frente al promedio)."""
    col = {"home_win": "consensus_home_odds", "draw": "consensus_draw_odds",
           "away_win": "consensus_away_odds", "over25": "consensus_over25_odds",
           "under25": "consensus_under25_odds"}.get(key)
    try:
        v = float(f.get(col)) if col else None
    except (TypeError, ValueError):
        return None
    return v if v and math.isfinite(v) and v > 1 else None


# ─────────────────────────────────────────────────────────────────────────────
# Veredicto de un momio
# ─────────────────────────────────────────────────────────────────────────────

def judge(price_decimal: float, market_p: float | None, model_p: float | None) -> dict:
    """
    Misma regla que el resto del sistema: probabilidad conservadora = la más
    baja entre el programa y el mercado sin margen; vale la pena si
    p − 1/momio ≥ 2 pts (src/utils/min_odds.py).
    """
    if market_p is None and model_p is None:
        return {"verdict": "sin_referencia"}
    p = min(x for x in (market_p, model_p) if x is not None)
    mn = min_american(p)
    edge = p - 1 / price_decimal
    ok = edge >= MIN_EDGE_TO_PLACE - 1e-9
    return {"verdict": "apuesta" if ok else "no", "prob": p, "min_american": mn,
            "ev": p * price_decimal - 1}


def evaluate_items(items: list, fixtures: list, normalize=None, bias: dict | None = None) -> list:
    """Cada momio leído de la captura con su veredicto. fixtures: partidos
    próximos con momios y modelo (ver reference_probs); bias: sesgo aprendido."""
    out = []
    for it in items or []:
        price = parse_price(it.get("momio"))
        f = find_fixture(it.get("local", ""), it.get("visitante", ""), fixtures, normalize)
        key = market_key(it)
        row = {"item": it, "price": price, "fixture": f, "key": key}
        if price is None:
            row["verdict"] = "ilegible"
        elif f is None:
            row["verdict"] = "sin_partido"
        elif key is None:
            row["verdict"] = "sin_mercado"
        else:
            market, model = reference_probs(f, bias)
            row.update(judge(price, market.get(key), model.get(key)))
            row["market_p"], row["model_p"] = market.get(key), model.get(key)
            row["consensus"] = consensus_decimal(f, key)
        out.append(row)
    return out


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_reply(rows: list) -> str:
    """Respuesta de Telegram (HTML) agrupada por partido."""
    if not rows:
        return ("🔎 No encontré momios en la captura. Manda la pantalla del partido en PlayDoit "
                "con los momios a la vista.")
    lines = ["🔎 <b>PlayDoit — lo que vi en tu captura</b>"]
    by_match, unknown = {}, []
    for r in rows:
        if r.get("fixture") is None:
            unknown.append(r)
            continue
        by_match.setdefault(r["fixture"]["label"], []).append(r)
    for match, rs in by_match.items():
        lines += ["", f"<b>{_esc(match)}</b>"]
        f = rs[0]["fixture"]
        for r in rs:
            price_us = fmt_american(to_american(r["price"])) if r.get("price") else str(r["item"].get("momio"))
            boost = " 🚀" if r["item"].get("mejorado") else ""
            if r.get("key"):
                label = LABELS[r["key"]].format(h=f["home"], a=f["away"])
            else:
                label = str(r["item"].get("descripcion") or r["item"].get("seleccion") or "")
            if r["verdict"] == "apuesta":
                lines.append(f"✅ {_esc(label)} {price_us}{boost}: vale la pena (mínimo "
                             f"{fmt_american(r['min_american'])}, valor estimado {r['ev'] * 100:+.1f}%)")
            elif r["verdict"] == "no":
                mn = fmt_american(r["min_american"]) if r.get("min_american") is not None else "—"
                lines.append(f"❌ {_esc(label)} {price_us}{boost}: no (necesita {mn} o mejor)")
            elif r["verdict"] == "sin_mercado":
                lines.append(f"➖ {_esc(label)} {price_us}: mercado sin referencia en el sistema")
            elif r["verdict"] == "sin_referencia":
                lines.append(f"➖ {_esc(label)} {price_us}: sin precio de referencia todavía")
            elif r["verdict"] == "ilegible":
                lines.append(f"➖ {_esc(label)}: no pude leer el momio")
    if unknown:
        names = sorted({f"{r['item'].get('local', '?')} vs {r['item'].get('visitante', '?')}" for r in unknown})
        lines += ["", "No están en mis partidos de los próximos días: " + _esc("; ".join(names))]
    lines += ["", "<i>Número más alto = paga más: -120 es mejor que -140 y +150 mejor que +130. "
                  "Apuesta solo lo que tenga ✅.</i>"]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Base de datos
# ─────────────────────────────────────────────────────────────────────────────

def load_fixtures(engine, hours_back: int = 2, days_ahead: int = 4) -> list:
    """Partidos próximos con momios (upcoming_matches) y el último modelo
    (model_vs_sharp)."""
    import pandas as pd
    up = pd.read_sql(text("""
        SELECT DISTINCT ON (home_team_norm, away_team_norm)
               home_team_norm, away_team_norm, home_team, away_team, sport_key, match_date,
               pin_home_odds, pin_draw_odds, pin_away_odds, consensus_home_odds, consensus_draw_odds,
               consensus_away_odds, consensus_over25_odds, consensus_under25_odds,
               over25_odds, under25_odds, btts_yes_odds, btts_no_odds
        FROM upcoming_matches
        WHERE match_date BETWEEN NOW() - make_interval(hours => :hb) AND NOW() + make_interval(days => :da)
        ORDER BY home_team_norm, away_team_norm, updated_at DESC NULLS LAST
    """), engine, params={"hb": hours_back, "da": days_ahead})
    try:
        mv = pd.read_sql(text("""
            SELECT DISTINCT ON (match) match, p_home, p_draw, p_away, p_over25, p_btts
            FROM model_vs_sharp WHERE match_date > NOW() - make_interval(hours => :hb) AND skipped_at IS NULL
            ORDER BY match, created_at DESC
        """), engine, params={"hb": hours_back})
        model = {str(r["match"]).lower(): r for r in mv.to_dict("records")}
    except Exception:
        model = {}
    out = []
    for r in up.to_dict("records"):
        key = f"{r['home_team_norm']} vs {r['away_team_norm']}".lower()
        m = model.get(key, {})
        out.append({**r, **{k: m.get(k) for k in ("p_home", "p_draw", "p_away", "p_over25", "p_btts")},
                    "league": r["sport_key"],          # la región de su sesgo (side_bias.bias_for)
                    "home_norm": str(r["home_team_norm"]).lower(), "away_norm": str(r["away_team_norm"]).lower(),
                    "home": r["home_team"], "away": r["away_team"],
                    "label": f"{r['home_team']} vs {r['away_team']}"})
    return out


def save_rows(engine, rows: list, message_id=None) -> int:
    """Guarda los momios leídos (solo los que se pudieron evaluar)."""
    import json
    saved = 0
    with engine.begin() as conn:
        conn.execute(text(DDL))
        for r in rows:
            f = r.get("fixture")
            if not f or not r.get("price") or not r.get("key"):
                continue
            conn.execute(text(f"""
                INSERT INTO {TABLE} (message_id, match, kickoff, league, market, line, american, decimal,
                                     boosted, consensus_decimal, pin_fair, model_prob, min_american,
                                     verdict, raw)
                VALUES (:mid, :match, :kick, :league, :market, :line, :am, :dec, :boost, :cons, :pin,
                        :model, :minam, :verdict, CAST(:raw AS jsonb))
            """), {"mid": message_id, "match": f"{f['home_norm']} vs {f['away_norm']}",
                   "kick": f.get("match_date"), "league": f.get("sport_key"), "market": r["key"],
                   "line": r["item"].get("linea"), "am": to_american(r["price"]), "dec": r["price"],
                   "boost": bool(r["item"].get("mejorado")), "cons": r.get("consensus"),
                   "pin": r.get("market_p"), "model": r.get("model_p"), "minam": r.get("min_american"),
                   "verdict": r.get("verdict"), "raw": json.dumps(r["item"], ensure_ascii=False)})
            saved += 1
    return saved


def playdoit_ratio(rows: list, min_n: int = RATIO_MIN_N) -> float | None:
    """Mediana de (momio PlayDoit / promedio de las casas) en momios no
    mejorados. rows: [{"decimal", "consensus_decimal", "boosted"}]. None
    sin muestra suficiente."""
    r = sorted(float(x["decimal"]) / float(x["consensus_decimal"]) for x in rows
               if not x.get("boosted") and x.get("decimal") and x.get("consensus_decimal"))
    if len(r) < min_n:
        return None
    mid = len(r) // 2
    return r[mid] if len(r) % 2 else (r[mid - 1] + r[mid]) / 2
