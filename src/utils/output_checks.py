"""
src/utils/output_checks.py
==========================
Chequeo diario de las SALIDAS del sistema (21-oct-26).

Las auditorías revisaban que cada paso corriera (flujos en verde, sin
excepciones) y los errores graves corrían "bien" con números equivocados:
el xG de Understat como total de la temporada tuvo 3 semanas el modelo en
el tope de goles en las 5 grandes ligas sin que nada fallara. Esto revisa
que lo que sale tenga sentido y avisa como ERROR (llega a Telegram como
"errores tolerados") el mismo día:

  - modelo: goles esperados en el tope, distancia a Pinnacle (pipeline_invariants,
    al final de cada corrida del pipeline);
  - liquidación: cada resultado de los últimos días se vuelve a calcular con
    una implementación propia, sin resolve_market (settlement_mismatches);
  - resultados: partidos analizados que ya se jugaron y no tienen marcador;
  - pendientes atorados más allá de la ventana de 'stale';
  - xG real fuera de escala;
  - lo aprendido: claves del weekly sin actualizar en 9+ días;
  - cierres: partidos de ayer con un cierre válido (29-oct-26).

Las funciones de revisión son puras; run_output_checks hace las consultas.
"""

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.utils.log import get_logger

log = get_logger(__name__)

LAMBDA_CAP_HIT = 3.49
MAX_CAP_SHARE = 0.05          # partidos con goles esperados en el tope
FAR_FROM_PIN = 0.20           # un resultado del 1X2 a 20+ pts de Pinnacle
MAX_FAR_SHARE = 0.10
MAX_MEDIAN_DIST = 0.09
MIN_ROWS = 20                 # con menos partidos no se juzga
MAX_MISSING_RESULTS = 0.15
MAX_XG_PER_MATCH = 4.0
WEEKLY_KEYS = ("anchor_weights", "side_bias", "calibration_factors", "dc_params",
               "sharp_reference", "strategy_lab", "rule_evidence")
WEEKLY_MAX_AGE_DAYS = 9
RESOLVED = ("win", "loss", "push", "half_win", "half_loss")


# ─────────────────────────────────────────────────────────────────────────────
# Modelo
# ─────────────────────────────────────────────────────────────────────────────

def pipeline_invariants(records: list) -> list:
    """records: filas de model_vs_sharp de una corrida (p_*, pin_*, lam_*).
    Devuelve los avisos (vacío = todo en orden)."""
    rows = [r for r in records or [] if r]
    if len(rows) < MIN_ROWS:
        return []
    out = []
    lams = [r for r in rows if r.get("lam_home") is not None and r.get("lam_away") is not None]
    if len(lams) >= MIN_ROWS:
        capped = sum(1 for r in lams if max(r["lam_home"], r["lam_away"]) >= LAMBDA_CAP_HIT)
        if capped / len(lams) > MAX_CAP_SHARE:
            out.append(f"Modelo: {capped} de {len(lams)} partidos con goles esperados en el tope "
                       f"(3.5) — algún dato de entrada está fuera de escala")
    dists = []
    for r in rows:
        try:
            dists.append(max(abs(float(r[f"p_{s}"]) - float(r[f"pin_{s}"])) for s in ("home", "draw", "away")))
        except (KeyError, TypeError, ValueError):
            pass
    if len(dists) >= MIN_ROWS:
        far = sum(1 for d in dists if d > FAR_FROM_PIN)
        med = sorted(dists)[len(dists) // 2]
        if far / len(dists) > MAX_FAR_SHARE:
            out.append(f"Modelo: {far} de {len(dists)} partidos a más de {FAR_FROM_PIN * 100:.0f} pts de Pinnacle")
        if med > MAX_MEDIAN_DIST:
            out.append(f"Modelo: distancia típica a Pinnacle de {med * 100:.1f} pts (normal: ~6)")
    return out


# Cobertura mínima de cada feature (23-oct-26). Por debajo, algo dejó de
# llegar. H2H se mide sobre los partidos evaluados (normal ~98%). xG,
# córners y tiros solo existen en las ligas que traen tiros en `matches`
# (with_shots, 27-oct-26): medidos sobre todos, una noche de Brasil y MLS
# daba "xG en 10 de 42" con los 10 de Serie A cubiertos. En esas ligas,
# desde agosto: xG 98% (la peor semana 90%), córners 93% (la peor 82%).
MIN_EVALUATED = 20
MIN_WITH_SHOTS = 10
MIN_COVERAGE = {"h2h_used": ("H2H", 0.25), "xg_used": ("xG", 0.60),
                "corners_used": ("córners", 0.50), "shots_used": ("tiros al arco", 0.50)}
NEEDS_SHOTS = ("xg_used", "corners_used", "shots_used")
MIN_XG_REAL_BIG5 = 0.80


def feature_coverage(stats: dict) -> list:
    """stats: contadores de una corrida del pipeline. Avisos de las features
    que casi no llegaron (vacío = en orden)."""
    n = int(stats.get("evaluated", 0))
    if n < MIN_EVALUATED:
        return []
    shots = int(stats.get("with_shots") or 0)
    out = []
    for key, (name, floor) in MIN_COVERAGE.items():
        base, where = (shots, "partidos de ligas con tiros") if key in NEEDS_SHOTS else (n, "partidos evaluados")
        if base < (MIN_WITH_SHOTS if key in NEEDS_SHOTS else MIN_EVALUATED):
            continue
        k = int(stats.get(key, 0))
        if k < floor * base:
            out.append(f"Entrada: {name} en {k} de {base} {where} (lo normal es mucho más) — "
                       f"revisar su consulta o sus nombres")
    b5 = int(stats.get("big5", 0))
    if "with_shots" in stats and shots < b5:       # las 5 grandes traen tiros: el conjunto se rompió
        out.append(f"Entrada: {shots} partidos de ligas con tiros y {b5} de las 5 grandes — "
                   f"revisar xg_proxy.leagues_with_shots")
    real = int(stats.get("xg_real_big5", 0))
    if b5 >= 10 and real < MIN_XG_REAL_BIG5 * b5:
        out.append(f"Entrada: xG real de Understat en {real} de {b5} partidos de las 5 grandes — "
                   f"nombres de team_xg_real o la carga semanal")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Liquidación independiente
# ─────────────────────────────────────────────────────────────────────────────

def _num(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _ou(total, line, side):
    def one(L):
        if abs(total - L) < 1e-9:
            return 0
        return 1 if ((total > L) if side == "over" else (total < L)) else -1
    if abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9:
        return {2: "win", 1: "half_win", 0: "push", -1: "half_loss", -2: "loss"}[one(line - 0.25) + one(line + 0.25)]
    return {1: "win", 0: "push", -1: "loss"}[one(line)]


def _ah(margin, line, side):
    def one(L):
        d = margin + L
        if abs(d) < 1e-9:
            return 0
        return 1 if ((d > 0) == (side == "home")) else -1
    if abs(line * 4 - round(line * 4)) < 1e-9 and abs(line * 2 - round(line * 2)) > 1e-9:
        return {2: "win", 1: "half_win", 0: "push", -1: "half_loss", -2: "loss"}[one(line - 0.25) + one(line + 0.25)]
    return {1: "win", 0: "push", -1: "loss"}[one(line)]


def settle_independent(market: str, r: dict) -> str | None:
    """Resultado de un mercado con una implementación propia (sin
    resolve_market). None si falta el dato o el mercado no está cubierto.
    Tarjetas: amarilla = 1 y roja = 1, como la liquidación (21-oct-26)."""
    hg, ag = int(r["home_goals"]), int(r["away_goals"])
    simple = {"home_win": hg > ag, "draw": hg == ag, "away_win": hg < ag,
              "btts": hg > 0 and ag > 0, "btts_no": not (hg > 0 and ag > 0),
              "dc_1x": hg >= ag, "dc_x2": hg <= ag, "dc_12": hg != ag}
    if market in simple:
        return "win" if simple[market] else "loss"
    if market in ("dnb_home", "dnb_away"):
        return "push" if hg == ag else ("win" if (hg > ag) == (market == "dnb_home") else "loss")
    g = re.fullmatch(r"(over|under)(\d)(\d)", market)
    if g:
        return _ou(hg + ag, float(f"{g.group(2)}.{g.group(3)}"), g.group(1))
    g = re.fullmatch(r"(over|under)_(\d+(?:\.\d+)?)", market)
    if g:
        return _ou(hg + ag, float(g.group(2)), g.group(1))
    g = re.fullmatch(r"ah_(home|away)_([-+]?\d+(?:\.\d+)?)", market)
    if g:
        return _ah(hg - ag, float(g.group(2)), g.group(1))
    g = re.fullmatch(r"h1_(home|draw|away)", market)
    if g:
        h1, a1 = _num(r.get("home_goals_ht")), _num(r.get("away_goals_ht"))
        if h1 is None or a1 is None:
            return None
        res = "home" if h1 > a1 else ("draw" if h1 == a1 else "away")
        return "win" if res == g.group(1) else "loss"
    for prop, cols in (("corners", ("home_corners", "away_corners")), ("cards", ("home_yellow", "away_yellow"))):
        g = re.fullmatch(prop + r"_(over|under)_(\d+(?:\.\d+)?)", market)
        if g:
            a, b = _num(r.get(cols[0])), _num(r.get(cols[1]))
            if a is None or b is None or (prop == "corners" and a + b == 0):    # 0-0 en córners = sin dato
                return None
            reds = (_num(r.get("home_red")) or 0) + (_num(r.get("away_red")) or 0) if prop == "cards" else 0
            return _ou(a + b + reds, float(g.group(2)), g.group(1))
    return None


def _match_index(matches: list) -> dict:
    idx = defaultdict(list)
    for m in matches:
        idx[(str(m["h"]).lower(), str(m["a"]).lower())].append(m)
    return idx


def _find(idx, match: str, when, normalize=None):
    if " vs " not in str(match):
        return None
    h, a = str(match).split(" vs ", 1)
    if normalize:
        h, a = normalize(h), normalize(a)
    import pandas as pd
    d = pd.Timestamp(when)
    if pd.isna(d):
        return None
    d = (d.tz_convert("UTC") if d.tzinfo else d).date()
    cand = [m for m in idx.get((h.lower(), a.lower()), []) if abs((pd.Timestamp(m["d"]).date() - d).days) <= 1]
    if len({(m["home_goals"], m["away_goals"]) for m in cand}) != 1:
        return None
    return cand[0]


def settlement_mismatches(rows: list, matches: list, normalize=None) -> list:
    """rows: [{"table", "id", "match", "match_date", "market", "result"}] ya
    liquidadas; matches: [{"h", "a", "d", goles, 1T, córners, tarjetas}].
    Devuelve las que no coinciden con la liquidación independiente."""
    idx = _match_index(matches)
    bad = []
    for r in rows:
        m = _find(idx, r["match"], r["match_date"], normalize)
        if m is None:
            continue
        mine = settle_independent(r["market"], m)
        if mine is not None and mine != r["result"]:
            bad.append({**r, "expected": mine, "score": f"{m['home_goals']}-{m['away_goals']}"})
    return bad


def missing_results(analyzed: list, matches: list, normalize=None) -> list:
    """Partidos analizados (model_vs_sharp: match, match_date) sin marcador."""
    idx = _match_index(matches)
    return [a for a in analyzed if _find(idx, a["match"], a["match_date"], normalize) is None]


LEAGUE_MISSING_MIN_N = 4
LEAGUE_MISSING_SHARE = 0.50


def leagues_without_results(analyzed: list, missing: list) -> list:
    """Ligas a las que les falta la mitad o más de los marcadores (29-oct-26):
    el total puede pasar con una liga entera sin resultados (si se cae su
    fuente) porque las demás la diluyen. Devuelve [(liga, faltan, total)]."""
    total, miss = defaultdict(int), defaultdict(int)
    for a in analyzed:
        total[a.get("league")] += 1
    for m in missing:
        miss[m.get("league")] += 1
    return sorted((lg, miss[lg], n) for lg, n in total.items()
                  if n >= LEAGUE_MISSING_MIN_N and miss[lg] >= LEAGUE_MISSING_SHARE * n)


def stale_learning(updated: dict, now: datetime | None = None) -> list:
    """{clave: updated_at} → claves del weekly sin actualizar en 9+ días."""
    now = now or datetime.now(timezone.utc)
    out = []
    for k in WEEKLY_KEYS:
        ts = updated.get(k)
        if ts is None:
            out.append(f"Aprendizaje: '{k}' no existe")
            continue
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if now - ts > timedelta(days=WEEKLY_MAX_AGE_DAYS):
            out.append(f"Aprendizaje: '{k}' sin actualizar desde {ts:%d-%b} (el weekly no lo corrió)")
    return out


# Datos que alimentan al modelo (23-oct-26): cada clase de error que ya pasó,
# revisada cada día en vez de esperar a la próxima auditoría.
MAX_MIRROR_PAIRS = 3          # 2 partidos reales con fechas espejo y mismo marcador pasan (Belgica 2023)
FEED_MAX_AGE_DAYS = {         # tabla: (columna de fecha, días)
    "team_xg_real": ("updated_at", 9),      # Understat, weekly
    "club_elo": ("fetched_on", 45),         # ClubElo, semanal; bloqueado desde el 29-oct (clubelo.MAX_AGE_DAYS)
    "espn_matches": ("fetched_at", 3),      # copas, Europa, amistosos: evening
    # lo que producen las corridas de cada día (morning, evening, closing)
    "upcoming_matches": ("updated_at", 1),  # momios, cada closing
    "odds_history": ("captured_at", 1),     # fotos de momios
    "kalshi_history": ("captured_at", 2),   # referencia de Kalshi
    "model_vs_sharp": ("created_at", 2),    # el análisis del modelo
    "program_picks": ("picked_at", 1),      # los picks del dashboard
}

DATA_HEALTH_SQL = {
    # la fecha espejo (6-feb ↔ 2-jun) con los mismos equipos y marcador: el
    # cargador de FBref dejó 2,271 así en Argentina
    "mirror": """SELECT COUNT(*) FROM matches a JOIN matches b
                   ON LOWER(a.home_team) = LOWER(b.home_team) AND LOWER(a.away_team) = LOWER(b.away_team)
                  AND a.id < b.id AND EXTRACT(YEAR FROM a.date) = EXTRACT(YEAR FROM b.date)
                  AND EXTRACT(DAY FROM a.date) = EXTRACT(MONTH FROM b.date)
                  AND EXTRACT(MONTH FROM a.date) = EXTRACT(DAY FROM b.date)
                  AND EXTRACT(DAY FROM a.date) <> EXTRACT(MONTH FROM a.date)
                  AND a.home_goals = b.home_goals AND a.away_goals = b.away_goals
                 WHERE a.date >= CURRENT_DATE - 400""",
    # partido con resultado y fecha futura: día y mes cruzados
    "future": "SELECT COUNT(*) FROM matches WHERE home_goals IS NOT NULL AND date > CURRENT_DATE + 1",
    # el mismo partido dos veces (±1 día, mismo marcador, mismos equipos)
    "duplicate": """SELECT COUNT(*) FROM matches a JOIN matches b
                      ON LOWER(a.home_team) = LOWER(b.home_team) AND LOWER(a.away_team) = LOWER(b.away_team)
                     AND a.id < b.id AND ABS(a.date::date - b.date::date) <= 1
                     AND a.home_goals = b.home_goals AND a.away_goals = b.away_goals
                    WHERE a.date >= CURRENT_DATE - 120""",
}


def data_health(counts: dict, ages: dict) -> list:
    """counts: {"mirror", "future", "duplicate"}; ages: {tabla: días desde lo
    último, o None si está vacía o no se pudo leer}; una tabla que todavía no
    existe (instalación nueva) no viene en ages. Avisos (vacío = en orden)."""
    out = []
    if int(counts.get("mirror") or 0) > MAX_MIRROR_PAIRS:
        out.append(f"Datos: {counts['mirror']} partidos repetidos con el día y el mes cruzados "
                   f"(algún cargador lee mal las fechas; ver src/utils/dates.py)")
    if int(counts.get("future") or 0) > 0:
        out.append(f"Datos: {counts['future']} partidos con resultado y fecha futura (día y mes cruzados)")
    if int(counts.get("duplicate") or 0) > 0:
        out.append(f"Datos: {counts['duplicate']} partidos repetidos en los últimos 120 días")
    for table, age in ages.items():
        if table not in FEED_MAX_AGE_DAYS:
            continue
        max_age = FEED_MAX_AGE_DAYS[table][1]
        if age is None:
            out.append(f"Datos: {table} vacía o ilegible")
        elif age > max_age:
            out.append(f"Datos: {table} sin actualizar hace {age} días (máximo {max_age})")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Corrida diaria (evening)
# ─────────────────────────────────────────────────────────────────────────────

def live_score_mismatches(live: list, matches: list, normalize=None) -> list:
    """Función pura (26-oct-26): los marcadores finales de ESPN del día
    (live_scores, el resultado provisional del dashboard) que no coinciden con
    el oficial de matches del mismo partido (±1 día). Uno distinto es un
    partido de ESPN emparejado con otro o un dato malo: el dashboard habría
    dado por ganado o perdido un pick con el marcador equivocado."""
    import pandas as pd
    norm = normalize or (lambda t: t)
    idx = {}
    for m in matches:
        idx.setdefault((norm(m["h"]), norm(m["a"])), []).append(m)
    out = []
    for r in live:
        if r.get("status") != "final" or r.get("home_goals") is None:
            continue
        day = pd.Timestamp(r["kickoff"]).tz_convert("UTC").date() if pd.Timestamp(r["kickoff"]).tzinfo \
            else pd.Timestamp(r["kickoff"]).date()
        for m in idx.get((norm(r["home"]), norm(r["away"])), []):
            if abs((pd.Timestamp(m["d"]).date() - day).days) > 1:
                continue
            if (int(m["home_goals"]), int(m["away_goals"])) != (int(r["home_goals"]), int(r["away_goals"])):
                out.append({"match": f"{r['home']} vs {r['away']}", "espn": f"{r['home_goals']}-{r['away_goals']}",
                            "oficial": f"{m['home_goals']}-{m['away_goals']}"})
            break
    return out


ESPN_FINALS_MIN = 0.85          # partidos de ligas de ESPN con su final (córners y tarjetas) guardado


def espn_final_coverage(matches: list, finals: list, leagues: set, normalize=None) -> dict:
    """Función pura (v2.5.1, 26-oct-26, el dueño: "¿por qué solo los de hoy
    tienen tarjetas y córners?"): de los partidos jugados de las ligas que
    cubre ESPN, cuántos tienen su final en live_scores (mismo partido ±1 día):
    de ahí salen los córners y las tarjetas que `matches` no trae."""
    import pandas as pd
    norm = normalize or (lambda t: t)
    have = {}
    for f in finals:
        have.setdefault((norm(f["home"]), norm(f["away"])), []).append(pd.Timestamp(f["kickoff"]))
    out = {"total": 0, "covered": 0, "missing": []}
    for m in matches:
        if m.get("league") not in leagues:
            continue
        out["total"] += 1
        d = pd.Timestamp(m["d"]).date()
        ks = have.get((norm(m["h"]), norm(m["a"])), [])
        if any(abs(((k.tz_convert("UTC") if k.tzinfo else k).date() - d).days) <= 1 for k in ks):
            out["covered"] += 1
        else:
            out["missing"].append(f"{m['h']} vs {m['a']}")
    return out


def counted_twice(rows: list, window_days: int = 3) -> list:
    """Función pura (27-oct-26, PSG vs Lyon pasó del 25 al 26): el mismo
    partido con fila válida en dos fechas a window_days días o menos, en la
    misma tabla. rows: [{"table", "match", "match_date"}] liquidadas (o
    válidas, en model_vs_sharp). bets_history no: dos apuestas son dos."""
    import pandas as pd
    by = {}
    for r in rows:
        if r.get("table") == "bets_history":
            continue
        t = pd.Timestamp(r["match_date"])
        by.setdefault((r["table"], r["match"]), set()).add(t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC"))
    out = []
    for (table, match), ds in sorted(by.items()):
        ds = sorted(ds)
        if any(b - a <= pd.Timedelta(days=window_days) for a, b in zip(ds, ds[1:])):
            out.append(f"{table}: {match} ({', '.join(f'{d:%d-%b %H:%M}' for d in ds)})")
    return out


STAR_MIN_N = 30                  # con menos picks 🎯 liquidados una familia no se juzga
STAR_SHOW_N = 10                 # desde aquí sale en la línea de ✅
# desviaciones por debajo de lo que esperaba el mercado. Medido el 28-oct-26
# sobre toda la historia, el z más bajo con 30+ picks fue +0.35 (Ambos
# anotan) y +1.18 (Ganador); Goles iba en -0.78 con 28.
STAR_Z_ALARM = -2.5


def star_vs_market(picks: list) -> list:
    """Función pura (28-oct-26: los goles 🎯 iban 12 de 27 contra 54%
    esperado y nada lo vigilaba). Por familia, los picks 🎯 liquidados
    (reason NULL, win/loss) contra la probabilidad del mercado sin margen
    (market_prob). Devuelve [{"family", "n", "won", "expected", "z"}], de más
    a menos picks. Un z muy negativo con suficientes picks: el 🎯 de esa
    familia elige peor que el mercado, o una probabilidad salió volteada."""
    by = {}
    for p in picks:
        mp = _num(p.get("market_prob"))
        if isinstance(p.get("reason"), str) or p.get("result") not in ("win", "loss") or mp is None:
            continue
        s = by.setdefault(p.get("family") or "?", {"n": 0, "won": 0, "expected": 0.0, "var": 0.0})
        s["n"] += 1
        s["won"] += p["result"] == "win"
        s["expected"] += mp
        s["var"] += mp * (1 - mp)
    out = [{"family": f, "n": s["n"], "won": s["won"], "expected": s["expected"],
            "z": (s["won"] - s["expected"]) / math.sqrt(s["var"]) if s["var"] > 0 else 0.0}
           for f, s in by.items()]
    return sorted(out, key=lambda s: -s["n"])


ODDS_FEEDS = ("upcoming_matches", "odds_history")   # se quedan quietas sin créditos de The Odds API


def credits_out_today(load_state=None, now: datetime | None = None) -> bool:
    """¿Ya salió hoy el aviso de "sin créditos" (credit_budget.alert_once)?
    Lo que falta por eso (cuotas, cierres) ya se avisó: no es otro error.
    Un día después de la renovación ya no vale."""
    try:
        if load_state is None:
            from src.utils.model_state import load_state
        from src.utils.credit_budget import ALERT_KEY
        now = now or datetime.now(timezone.utc)
        return (load_state(ALERT_KEY) or {}).get("stop") == now.strftime("%Y-%m-%d")
    except Exception:
        return False


CLOSING_MIN_SHARE = 0.80         # normal medido: 98-100% (9-23 oct); 24-25 oct cayó a 15-19%
CLOSING_MIN_N = 10


def closing_coverage(rows: list) -> dict:
    """Partidos sombra jugados con un cierre válido (descargado poco antes del
    kickoff, closing_quality). `rows`: {match, valid} por candidata; un
    partido cuenta como cubierto si alguna de sus candidatas lo tiene."""
    by = defaultdict(bool)
    for r in rows:
        by[r["match"]] |= bool(r.get("valid"))
    missing = sorted(m for m, ok in by.items() if not ok)
    return {"total": len(by), "covered": len(by) - len(missing), "missing": missing}


LINEUP_SINCE = "2026-10-27"      # desde aquí el pick se recalcula con la alineación (v2.5.0)
UNSEEN_MAX = 0.30                # más picks sin ver su alineación publicada que esto: el paso falla


def lineup_pick_problems(picks: list, lineups: list, profiles: dict) -> dict:
    """Función pura (26-oct-26, el dueño: "no es correcto poner que un equipo
    ganará cuando no tiene a jugadores importantes"). picks: program_picks
    jugados (match, match_date, market, reason, lineup); lineups: filas de
    `lineups` (team, opponent, is_home, kickoff, starters) de esos partidos;
    profiles: {equipo: team_profile}. Devuelve {"weak_rule": picks 🎯 a que
    gana un equipo al que le faltaron 4+ titulares habituales, "unseen": picks
    con las dos alineaciones publicadas que no las vieron, "with_lineup": de
    cuántos}."""
    import json
    import pandas as pd
    from src.models.lineup_alerts import TRIGGER_MISSING, absences
    idx = {}
    for r in lineups:
        home, away = (r["team"], r["opponent"]) if r["is_home"] else (r["opponent"], r["team"])
        xi = r["starters"] if isinstance(r["starters"], list) else json.loads(r["starters"] or "[]")
        idx.setdefault(f"{home} vs {away}", []).append((pd.Timestamp(r["kickoff"]), r["team"], xi))
    out = {"weak_rule": [], "unseen": [], "with_lineup": 0}
    for p in picks:
        k = pd.Timestamp(p["match_date"])
        got = {t: xi for kk, t, xi in idx.get(p["match"], []) if abs((kk - k).total_seconds()) <= 3 * 3600}
        home, _, away = str(p["match"]).partition(" vs ")
        if home not in got or away not in got:
            continue
        out["with_lineup"] += 1
        if not isinstance(p.get("lineup"), str):
            out["unseen"].append(p["match"])
        team = {"home_win": home, "away_win": away}.get(p.get("market"))
        if team and not isinstance(p.get("reason"), str) and team in profiles:
            a = absences(got[team], profiles[team])
            if a["known"] and len(a["missing"]) >= TRIGGER_MISSING:
                out["weak_rule"].append(f"{p['match']} ({team} sin {len(a['missing'])})")
    return out


def run_output_checks(engine=None, normalize=None, days: int = 3) -> list:
    """Corre todo y avisa cada problema como ERROR. Devuelve los avisos."""
    import pandas as pd
    if engine is None:
        from config.database import engine
    if normalize is None:
        from src.utils.team_normalizer import normalize_team as normalize
    problems = []

    def q(sql, **p):
        return pd.read_sql(text(sql), engine, params=p)

    matches = q("""
        SELECT LOWER(home_team) AS h, LOWER(away_team) AS a, date::date AS d, home_goals, away_goals,
               home_goals_ht, away_goals_ht, home_corners, away_corners, home_yellow, away_yellow,
               home_red, away_red
        FROM matches WHERE home_goals IS NOT NULL AND date >= CURRENT_DATE - make_interval(days => :d)
    """, d=days + 3).to_dict("records")

    # 1) liquidación de lo resuelto en los últimos días
    rows = []
    for table in ("bets_history", "shadow_bets", "model_proposals", "program_picks"):
        try:
            df = q(f"""SELECT id, match, match_date, market, result FROM {table}
                       WHERE result IN {RESOLVED} AND match_date >= NOW() - make_interval(days => :d)""", d=days)
            rows += [{**r, "table": table} for r in df.to_dict("records")]
        except Exception as e:
            if table == "program_picks" and "does not exist" in str(e):
                continue                  # nace con el primer pick guardado (21-oct-26)
            problems.append(f"Liquidación: no se pudo leer {table} ({type(e).__name__})")
    bad = settlement_mismatches(rows, matches, normalize)
    if bad:
        ex = "; ".join(f"{b['table']} #{b['id']} {b['market']} {b['score']}: {b['result']}≠{b['expected']}"
                       for b in bad[:3])
        problems.append(f"Liquidación: {len(bad)} resultados no coinciden con el cálculo independiente ({ex})")

    # 2) resultados que faltan
    try:
        an = q("""SELECT match, match_date, league FROM model_vs_sharp
                  WHERE match_date BETWEEN NOW() - make_interval(days => :d) AND NOW() - INTERVAL '8 hours'""",
               d=days).to_dict("records")
        miss = missing_results(an, matches, normalize)
        if len(an) >= MIN_ROWS and len(miss) / len(an) > MAX_MISSING_RESULTS:
            problems.append(f"Resultados: {len(miss)} de {len(an)} partidos analizados sin marcador "
                            f"({', '.join(m['match'] for m in miss[:3])}…)")
        for lg, n_miss, n in leagues_without_results(an, miss):
            problems.append(f"Resultados: a {lg} le faltan {n_miss} de {n} marcadores (¿se cayó su fuente?)")
    except Exception as e:
        problems.append(f"Resultados: no se pudo revisar ({type(e).__name__})")

    # 3) pendientes atorados más allá de la ventana de 'stale'
    try:
        st = q("""SELECT
            (SELECT COUNT(*) FROM bets_history WHERE result IN ('pending', 'unresolved')
                AND match_date < NOW() - INTERVAL '8 days') AS bets,
            (SELECT COUNT(*) FROM shadow_bets WHERE result IS NULL AND match_date < NOW() - INTERVAL '11 days') AS shadow,
            (SELECT COUNT(*) FROM model_proposals WHERE result IS NULL AND match_date < NOW() - INTERVAL '11 days') AS props
        """).iloc[0]
        for name, n in (("apuestas reales", st.bets), ("sombra", st.shadow), ("propuestas", st.props)):
            if int(n or 0) > 0:
                problems.append(f"Pendientes: {int(n)} {name} atoradas más allá de su ventana")
    except Exception as e:
        problems.append(f"Pendientes: no se pudo revisar ({type(e).__name__})")
    try:                                          # los picks también pasan a 'stale' a los 10 días (29-oct-26)
        n = int(q("""SELECT COUNT(*) AS n FROM program_picks WHERE result IS NULL AND market IS NOT NULL
                     AND match_date < NOW() - INTERVAL '11 days'""").iloc[0]["n"] or 0)
        if n > 0:
            problems.append(f"Pendientes: {n} picks del programa atorados más allá de su ventana")
    except Exception as e:
        if "does not exist" not in str(e):        # la tabla nace con el primer pick guardado
            problems.append(f"Pendientes: no se pudo revisar los picks ({type(e).__name__})")

    # 4) xG real fuera de escala
    try:
        mx = q("""SELECT MAX(xg_for / NULLIF(matches, 0)) AS m FROM team_xg_real
                  WHERE season = (SELECT MAX(season) FROM team_xg_real)""").iloc[0]["m"]
        if mx is not None and float(mx) > MAX_XG_PER_MATCH:
            problems.append(f"xG real: {float(mx):.1f} por partido (fuera de escala)")
    except Exception:
        pass

    # 5) lo aprendido por el weekly
    try:
        st = q("SELECT key, updated_at FROM model_state WHERE key = ANY(:k)", k=list(WEEKLY_KEYS))
        problems += stale_learning({r.key: pd.Timestamp(r.updated_at).to_pydatetime() for r in st.itertuples()})
    except Exception as e:
        problems.append(f"Aprendizaje: no se pudo revisar ({type(e).__name__})")

    # 6) los datos que alimentan al modelo
    try:
        counts = {k: int(q(sql).iloc[0, 0]) for k, sql in DATA_HEALTH_SQL.items()}
        ages = {}
        for table, (col, _) in FEED_MAX_AGE_DAYS.items():
            try:
                last = q(f"SELECT MAX({col}) AS t FROM {table}").iloc[0]["t"]
                ages[table] = None if last is None or pd.isna(last) else                     (pd.Timestamp.now(tz="UTC") - (pd.Timestamp(last).tz_convert("UTC") if pd.Timestamp(last).tzinfo
                                                   else pd.Timestamp(last).tz_localize("UTC"))).days
            except Exception as e:
                if "does not exist" not in str(e):     # la tabla nace con su primera corrida
                    ages[table] = None
        found = data_health(counts, ages)
        quiet = [p for p in found if any(f"Datos: {t} sin actualizar" in p for t in ODDS_FEEDS)]
        if quiet and credits_out_today():         # 29-oct-26: la causa ya se avisó una vez hoy
            for p in quiet:
                log.warning(f"⚠️  Chequeo de salidas — {p}: sin créditos de The Odds API (ya avisado)")
            found = [p for p in found if p not in quiet]
        problems += found
    except Exception as e:
        problems.append(f"Datos: no se pudo revisar ({type(e).__name__})")

    # 7) el marcador provisional del dashboard (ESPN) contra el oficial
    try:
        live = q("""SELECT home, away, kickoff, home_goals, away_goals, status FROM live_scores
                    WHERE kickoff >= NOW() - make_interval(days => :d)""", d=days).to_dict("records")
        bad = live_score_mismatches(live, matches, normalize)
        if bad:
            problems.append(f"Marcadores del día: {len(bad)} de ESPN no coinciden con el oficial ("
                            + "; ".join(f"{b['match']} {b['espn']}≠{b['oficial']}" for b in bad[:3]) + ")")
    except Exception as e:
        if "does not exist" not in str(e):              # la tabla nace con el primer closing
            problems.append(f"Marcadores del día: no se pudo revisar ({type(e).__name__})")

    # 8) el pick con la alineación (v2.5.0): ningún 🎯 a un equipo con 4+
    #    titulares fuera, y los picks ven la alineación cuando ESPN la publica
    seen = []                                     # lo revisado, para la línea de ✅
    try:
        pk = q("""SELECT match, match_date, market, reason, lineup FROM program_picks
                  WHERE match_date BETWEEN GREATEST(NOW() - make_interval(days => :d), CAST(:since AS timestamptz))
                                       AND NOW() AND market IS NOT NULL""", d=days, since=LINEUP_SINCE).to_dict("records")
        if pk:
            lu = q("""SELECT team, opponent, is_home, kickoff, starters FROM lineups
                      WHERE kickoff BETWEEN GREATEST(NOW() - make_interval(days => :d), CAST(:since AS timestamptz))
                                        - INTERVAL '3 hours' AND NOW()""", d=days, since=LINEUP_SINCE).to_dict("records")
            from src.models.lineup_alerts import load_profiles
            lp = lineup_pick_problems(pk, lu, load_profiles(engine, sorted({r["team"] for r in lu})))
            seen.append(f"alineaciones ({lp['with_lineup'] - len(lp['unseen'])} de {lp['with_lineup']} picks la vieron)")
            if lp["weak_rule"]:
                problems.append(f"Picks y alineaciones: {len(lp['weak_rule'])} 🎯 a que gana un equipo con 4+ "
                                f"titulares fuera ({'; '.join(lp['weak_rule'][:3])})")
            if lp["with_lineup"] >= 10 and len(lp["unseen"]) > UNSEEN_MAX * lp["with_lineup"]:
                problems.append(f"Picks y alineaciones: {len(lp['unseen'])} de {lp['with_lineup']} picks no vieron "
                                f"la alineación publicada (el closing no los recalculó; p. ej. {lp['unseen'][0]})")
    except Exception as e:
        if "does not exist" not in str(e):        # la tabla y la columna lineup nacen con el closing
            problems.append(f"Picks y alineaciones: no se pudo revisar ({type(e).__name__})")

    # 9) córners y tarjetas de ESPN de lo jugado (v2.5.1): el evening los guarda
    try:
        from src.features.espn_lineups import espn_leagues
        played = q("""SELECT LOWER(home_team) AS h, LOWER(away_team) AS a, date::date AS d, league FROM matches
                      WHERE home_goals IS NOT NULL AND date BETWEEN CURRENT_DATE - make_interval(days => :d)
                                                               AND CURRENT_DATE - 1""", d=days).to_dict("records")
        fin = q("""SELECT home, away, kickoff FROM live_scores WHERE status = 'final'
                   AND kickoff >= CURRENT_DATE - make_interval(days => :d)""", d=days + 2).to_dict("records")
        cov = espn_final_coverage(played, fin, set(espn_leagues()), normalize)
        seen.append(f"finales de ESPN ({cov['covered']} de {cov['total']})")
        if cov["total"] >= 10 and cov["covered"] < ESPN_FINALS_MIN * cov["total"]:
            problems.append(f"Córners y tarjetas (ESPN): solo {cov['covered']} de {cov['total']} partidos jugados "
                            f"tienen su final de ESPN (p. ej. {', '.join(cov['missing'][:3])})")
    except Exception as e:
        if "does not exist" not in str(e):
            problems.append(f"Córners y tarjetas (ESPN): no se pudo revisar ({type(e).__name__})")

    # 10) un partido reprogramado no cuenta dos veces (27-oct-26): la fecha
    #     vieja se llevaba el resultado del real (src/models/rescheduled.py)
    try:
        valid = q("""SELECT match, match_date FROM model_vs_sharp WHERE skipped_at IS NULL
                     AND match_date >= NOW() - make_interval(days => :d)""", d=days + 3).to_dict("records")
        twice = counted_twice(rows + [{**r, "table": "model_vs_sharp"} for r in valid])
        seen.append(f"reprogramados contados doble ({len(twice)})")
        if twice:
            problems.append(f"Reprogramados: {len(twice)} partidos cuentan en dos fechas ({'; '.join(twice[:3])})")
    except Exception as e:
        problems.append(f"Reprogramados: no se pudo revisar ({type(e).__name__})")

    # 11) el 🎯 contra el mercado (28-oct-26): una familia que gana muy por
    #     debajo de lo que esperaba el mercado está eligiendo peor que él
    try:
        st = star_vs_market(q("""SELECT family, reason, result, market_prob FROM program_picks
                                 WHERE market IS NOT NULL AND result IN ('win', 'loss')""").to_dict("records"))
        shown = [f"{s['family']} {s['won']} de {s['n']} (esperado {s['expected']:.1f})"
                 for s in st if s["n"] >= STAR_SHOW_N]
        if shown:
            seen.append("🎯 contra el mercado: " + ", ".join(shown))
        for s in st:
            if s["n"] >= STAR_MIN_N and s["z"] <= STAR_Z_ALARM:
                problems.append(f"🎯 contra el mercado: {s['family']} ganó {s['won']} de {s['n']} y el mercado "
                                f"esperaba {s['expected']:.1f} ({s['z']:+.1f} desviaciones): el 🎯 de esa familia "
                                f"elige peor que el mercado")
    except Exception as e:
        if "does not exist" not in str(e):        # la tabla nace con el primer pick guardado
            problems.append(f"🎯 contra el mercado: no se pudo revisar ({type(e).__name__})")

    # 12) cierres guardados (29-oct-26): del 24 al 28 de oct solo 0-53% de los
    #     partidos tuvo un cierre válido y nada avisó; sin cierres el
    #     aprendizaje del peso del modelo no recibe datos
    try:
        from src.utils.closing_quality import valid_closing_sql
        cc = closing_coverage(q(f"""SELECT match, (closing_odds IS NOT NULL AND {valid_closing_sql()}
                                                   AND closing_fetched_at > created_at) AS valid
                                    FROM shadow_bets
                                    WHERE match_date BETWEEN NOW() - INTERVAL '27 hours'
                                                         AND NOW() - INTERVAL '3 hours'""").to_dict("records"))
        seen.append(f"cierres ({cc['covered']} de {cc['total']} partidos)")
        if cc["total"] >= CLOSING_MIN_N and cc["covered"] < CLOSING_MIN_SHARE * cc["total"]:
            msg = (f"Cierres: solo {cc['covered']} de {cc['total']} partidos de ayer tienen un cierre válido "
                   f"(p. ej. {', '.join(cc['missing'][:3])})")
            if credits_out_today():               # el closing manda el aviso desde las 00:00 UTC
                log.warning(f"⚠️  Chequeo de salidas — {msg}: sin créditos de The Odds API (ya avisado)")
            else:
                problems.append(msg)
    except Exception as e:
        problems.append(f"Cierres: no se pudo revisar ({type(e).__name__})")

    for p in problems:
        log.error(f"❌ Chequeo de salidas — {p}")
    if not problems:
        print(f"✅ Chequeo de salidas: liquidación ({len(rows)} revisadas), resultados, pendientes, "
              f"xG, aprendizaje y datos de entrada en orden" + "".join(f" · {x}" for x in seen))
    return problems
