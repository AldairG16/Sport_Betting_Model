"""
src/models/edge_finder.py
=========================
Buscador de ventajas (19-oct-26, a pedido del dueño: "tenemos bastantes datos
para sacar estadísticas... debemos seguir buscando y ampliando las opciones").

Prueba ideas de apuesta ("el local que viene en racha", "los dos vienen
anotando → más de 2.5", "el recién ascendido de local", "el equipo con
suerte"...) contra los MOMIOS REALES de cierre de años de partidos
(historical_odds, scripts/load_historical_odds.py). La pregunta no es si el
equipo en racha gana más (sí gana más), sino si gana más de lo que ya pagan
las casas: el momio es la probabilidad de la casa, y solo hay ventaja donde
el momio se equivoca.

Cómo se juzga (medido el 19-oct: una idea que ganaba +10% y +15% en 2019-25
perdió −11% en 2012-19; con miles de datos siempre "aparece" algo):
  - tres periodos que se miran por separado: 2012-18, 2019-22 y 2023 en
    adelante;
  - el precio es el PROMEDIO entre casas, el más parecido a PlayDoit
    (medido el 19-oct: PlayDoit −2.2 pt contra Pinnacle, promedio −2.4);
  - PASA solo si gana en los tres periodos (≥ 50 apuestas cada uno) y la
    ganancia junta no puede ser suerte aun habiendo probado todas las ideas
    (Bonferroni: z ≥ z(1 − 0.05/k));
  - PROMETEDORA si gana en los tres pero todavía puede ser suerte.
  - una idea que NACIÓ de mirar este mismo historial (FROM_THIS_HISTORY) nunca
    "pasa" con él: la prueba ya vio la respuesta. Llega a prometedora y la
    confirman, o no, los partidos nuevos (favourite_watch). 12-nov-26:
    "visitante muy favorito sin suerte de gol" daba +11% con z 4.1 (303
    partidos), pero salió de explorar estos datos y ni el local (+1.4%), ni el
    visitante de 60 a 70 (+3.3%), ni las otras ligas (−4.7%) lo repiten.
Solo mide: pasar una idea al laboratorio y a dinero real es el paso siguiente.

Cada rasgo usa solo partidos ANTERIORES de la misma liga y temporada (la
racha, el descanso y la tabla de ese momento), nunca el resultado del propio
partido. La suerte de gol de 10 partidos (luck10, 12-nov-26) es la única que
cruza de una temporada a la siguiente, igual que la que mide en vivo
favourite_watch: mismas constantes, una sola definición.
"""

from datetime import datetime, timezone
from statistics import NormalDist

import numpy as np
import pandas as pd
from sqlalchemy import text

from src.models.big_names import (FORM_GAMES, GOOD_POINTS, MIN_LEAGUE_TEAMS, POOR_POINTS, REP_MIN_GAMES,
                                  REP_SEASONS, TOP_N)
from src.models.early_payout import PA_LEAGUES
from src.models.favourite_watch import LUCK_CUT, LUCK_GAMES, LUCK_K, LUCK_MIN

STATE_KEY = "edge_finder"
PERIODS = (("2012-18", 2012, 2018), ("2019-22", 2019, 2022), ("2023+", 2023, 9999))
MIN_N_PERIOD = 50
ALPHA = 0.05
LUCK_SHOT_GOALS = 0.3        # goles esperados por tiro a puerta (aprox.)
PINNACLE_GAP_DAYS = 60       # con más días sin precio de Pinnacle al final del historial, el reporte lo dice

PASSES, PROMISING, DISCARDED, THIN = "pasa", "prometedora", "descartada", "pocos datos"
# ideas que salieron de explorar este mismo historial: con él no pueden pasar
FROM_THIS_HISTORY = frozenset({"favorito_sin_suerte_local", "favorito_con_suerte_local", "favorito_sin_suerte_visita",
                               "grande_mal_local", "grande_bien_local", "sin_nombre_local", "grande_mal_visita",
                               "grande_bien_visita", "contra_grande_mal_local"})

# lado → (columna de precio sin prefijo, probabilidad justa de Pinnacle, ¿ganó?)
SIDES = {
    "H": ("home", "fh", lambda f: f["home_goals"] > f["away_goals"]),
    "D": ("draw", "fd", lambda f: f["home_goals"] == f["away_goals"]),
    "A": ("away", "fa", lambda f: f["home_goals"] < f["away_goals"]),
    "O": ("over25", "fo", lambda f: f["home_goals"] + f["away_goals"] > 2.5),
    "U": ("under25", "fu", lambda f: f["home_goals"] + f["away_goals"] < 2.5),
}

# (clave, idea en palabras del dueño, lado, filtro sobre los rasgos)
HYPOTHESES = (
    ("local_racha", "Local en racha (12+ puntos de 15)", "H", lambda f: f.h_pts5 >= 12),
    ("visita_racha", "Visitante en racha (12+ puntos de 15)", "A", lambda f: f.a_pts5 >= 12),
    ("local_racha_vs_mala", "Local en racha (10+) contra visitante en mala (4 o menos)", "H",
     lambda f: (f.h_pts5 >= 10) & (f.a_pts5 <= 4)),
    ("visita_racha_vs_mala", "Visitante en racha (10+) contra local en mala (4 o menos)", "A",
     lambda f: (f.a_pts5 >= 10) & (f.h_pts5 <= 4)),
    ("contra_local_racha", "Contra el local en racha: apostar al visitante", "A", lambda f: f.h_pts5 >= 12),
    ("contra_visita_racha", "Contra el visitante en racha: apostar al local", "H", lambda f: f.a_pts5 >= 12),
    ("local_mala_racha", "Local en mala racha (3 o menos): a que se recupera", "H", lambda f: f.h_pts5 <= 3),
    ("visita_mala_racha", "Visitante en mala racha (3 o menos): a que se recupera", "A", lambda f: f.a_pts5 <= 3),
    ("ambos_goleadores", "Los dos anotaron en 4+ de sus últimos 5 → más de 2.5", "O",
     lambda f: (f.h_scored5 >= 4) & (f.a_scored5 >= 4)),
    ("ambos_abiertos", "Los dos anotan y reciben en 4+ de 5 → más de 2.5", "O",
     lambda f: (f.h_scored5 >= 4) & (f.a_scored5 >= 4) & (f.h_conceded5 >= 4) & (f.a_conceded5 >= 4)),
    ("ambos_cerrados", "Los dos recibieron gol en 2 o menos de 5 → menos de 2.5", "U",
     lambda f: (f.h_conceded5 <= 2) & (f.a_conceded5 <= 2)),
    ("ambos_secos", "Los dos anotaron en 2 o menos de 5 → menos de 2.5", "U",
     lambda f: (f.h_scored5 <= 2) & (f.a_scored5 <= 2)),
    ("local_descansado", "Local descansado (7+ días) contra visitante cansado (4 o menos)", "H",
     lambda f: (f.h_rest >= 7) & (f.a_rest <= 4)),
    ("visita_descansada", "Visitante descansado (7+ días) contra local cansado (4 o menos)", "A",
     lambda f: (f.a_rest >= 7) & (f.h_rest <= 4)),
    ("local_cansado", "Local con 3 días o menos de descanso: apostar al visitante", "A", lambda f: f.h_rest <= 3),
    ("ascendido_local_contra", "Recién ascendido de local (primeros 8): apostar al visitante", "A",
     lambda f: f.h_promoted & (f.h_played < 8)),
    ("ascendido_local_favor", "Recién ascendido de local (primeros 8): apostar al local", "H",
     lambda f: f.h_promoted & (f.h_played < 8)),
    ("ascendido_visita_contra", "Recién ascendido de visitante (primeros 8): apostar al local", "H",
     lambda f: f.a_promoted & (f.a_played < 8)),
    ("ascendido_visita_favor", "Recién ascendido de visitante (primeros 8): apostar al visitante", "A",
     lambda f: f.a_promoted & (f.a_played < 8)),
    ("local_con_suerte", "Local que gana más de lo que genera: apostar al visitante", "A", lambda f: f.h_luck6 >= 4),
    ("visita_con_suerte", "Visitante que gana más de lo que genera: apostar al local", "H", lambda f: f.a_luck6 >= 4),
    ("local_sin_suerte", "Local que genera más de lo que gana: apostar al local", "H", lambda f: f.h_luck6 <= -4),
    ("visita_sin_suerte", "Visitante que genera más de lo que gana: apostar al visitante", "A",
     lambda f: f.a_luck6 <= -4),
    ("local_goleado", "Local que perdió por 3+ en su partido anterior: apostar al local", "H",
     lambda f: f.h_last <= -3),
    ("visita_goleada", "Visitante que perdió por 3+ en su partido anterior: apostar al visitante", "A",
     lambda f: f.a_last <= -3),
    ("local_goleo", "Local que ganó por 3+ en su partido anterior: apostar al visitante", "A",
     lambda f: f.h_last >= 3),
    ("descenso_local", "Últimas 6 fechas: local en zona de descenso", "H", lambda f: f.h_late & f.h_bottom3),
    ("descenso_visita", "Últimas 6 fechas: visitante en zona de descenso", "A", lambda f: f.a_late & f.a_bottom3),
    ("favorito_local_grande", "Local muy favorito (70%+ según Pinnacle)", "H", lambda f: f.fh >= 0.70),
    ("visitante_muy_debil", "Visitante con 15% o menos según Pinnacle", "A", lambda f: f.fa <= 0.15),
    ("empate_parejo", "Empate en partido parejo (local y visitante a 5 pt o menos)", "D",
     lambda f: (f.fh - f.fa).abs() <= 0.05),
    ("empate_pocos_goles", "Empate cuando el mercado espera pocos goles (menos de 2.5 al 60%+)", "D",
     lambda f: f.fu >= 0.60),
    # 26-oct-26, el dueño: "si un equipo es propenso a recibir muchos goles, que también
    # pienses en un pick de goles" (LAFC vs LA Galaxy, que recibía 2.9 por partido de visita)
    ("local_goleable", "Local que recibe 2+ goles por partido (últimos 5) → más de 2.5", "O",
     lambda f: f.h_ga5 >= 10),
    ("visita_goleable", "Visitante que recibe 2+ goles por partido (últimos 5) → más de 2.5", "O",
     lambda f: f.a_ga5 >= 10),
    ("alguno_goleable", "Alguno de los dos recibe 2+ goles por partido (últimos 5) → más de 2.5", "O",
     lambda f: (f.h_ga5 >= 10) | (f.a_ga5 >= 10)),
    ("goleable_contra_goleador", "Visitante que recibe 2+ contra local que anota 2+ (últimos 5) → más de 2.5", "O",
     lambda f: (f.a_ga5 >= 10) & (f.h_gf5 >= 10)),
    # al revés: si el mercado exagera esos goles, el menos de 2.5 tendría el valor
    ("alguno_goleable_menos", "Alguno de los dos recibe 2+ goles por partido (últimos 5) → menos de 2.5", "U",
     lambda f: (f.h_ga5 >= 10) | (f.a_ga5 >= 10)),
    # 12-nov-26: de repetir el historial buscando qué no trae el precio del muy favorito (el 🎯).
    # En las ligas con pago anticipado, el de 70%+ que NO viene metiendo más goles de los que
    # justifican sus tiros a puerta dejó +2.9, +2.7 y +3.4 por cada 100 en tres épocas; el que sí,
    # +2.2, −2.2 y −0.2. Aquí queda a prueba cada semana, con la regla de siempre.
    ("favorito_sin_suerte_local", "Local muy favorito (70%+, liga con pago anticipado) que no viene con suerte de gol",
     "H", lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fh >= 0.70) & (f.h_luck10 - f.a_luck10 <= LUCK_CUT)),
    ("favorito_con_suerte_local", "Local muy favorito (70%+, liga con pago anticipado) que viene con suerte de gol",
     "H", lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fh >= 0.70) & (f.h_luck10 - f.a_luck10 > LUCK_CUT)),
    ("favorito_sin_suerte_visita", "Visitante muy favorito (70%+, liga con pago anticipado) que no viene con "
     "suerte de gol", "A",
     lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fa >= 0.70) & (f.a_luck10 - f.h_luck10 <= LUCK_CUT)),
    # 13-nov-26, el dueño: "lista negra" de equipos grandes de nombre que vienen en decadencia (Chelsea,
    # Juventus, ManU). Grande = de los 4 que más ganaban según las casas en su liga en las dos temporadas
    # anteriores; cómo viene = puntos de sus últimos 10 de liga (big_names, mismas constantes). Medido a
    # mano: el que viene bien +1.4 por cada 100 en las tres épocas, el que viene mal −2.2 (pero +3.4 en
    # 2023-26) y el favorito sin gran nombre −3.0. Aquí queda a prueba cada semana.
    ("grande_mal_local", "Local muy favorito (65%+, liga con pago anticipado), grande de nombre que viene mal "
     "(15 puntos o menos de 30)", "H",
     lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fh >= 0.65) & (f.h_big == 1) & (f.h_pts10 <= POOR_POINTS)),
    ("grande_bien_local", "Local muy favorito (65%+, liga con pago anticipado), grande de nombre que viene bien "
     "(20 puntos o más de 30)", "H",
     lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fh >= 0.65) & (f.h_big == 1) & (f.h_pts10 >= GOOD_POINTS)),
    ("sin_nombre_local", "Local muy favorito (65%+, liga con pago anticipado) sin gran nombre", "H",
     lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fh >= 0.65) & (f.h_big == 0)),
    ("grande_mal_visita", "Visitante muy favorito (65%+, liga con pago anticipado), grande de nombre que viene mal "
     "(15 puntos o menos de 30)", "A",
     lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fa >= 0.65) & (f.a_big == 1) & (f.a_pts10 <= POOR_POINTS)),
    ("grande_bien_visita", "Visitante muy favorito (65%+, liga con pago anticipado), grande de nombre que viene bien "
     "(20 puntos o más de 30)", "A",
     lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fa >= 0.65) & (f.a_big == 1) & (f.a_pts10 >= GOOD_POINTS)),
    ("contra_grande_mal_local", "Contra el local grande de nombre que viene mal (muy favorito, 65%+): apostar al "
     "visitante", "A",
     lambda f: f.sport_key.isin(PA_LEAGUES) & (f.fh >= 0.65) & (f.h_big == 1) & (f.h_pts10 <= POOR_POINTS)),
)


# ─────────────────────────────────────────────────────────────────────────────
# Rasgos de cada partido (solo con partidos anteriores)
# ─────────────────────────────────────────────────────────────────────────────

def _fair(df: pd.DataFrame) -> pd.DataFrame:
    """Probabilidades sin margen de Pinnacle (proporcional)."""
    inv = 1.0 / df[["pin_home", "pin_draw", "pin_away"]].astype(float)
    tot = inv.sum(axis=1, min_count=3)
    out = pd.DataFrame({"fh": inv["pin_home"] / tot, "fd": inv["pin_draw"] / tot,
                        "fa": inv["pin_away"] / tot}, index=df.index)
    io, iu = 1.0 / df["pin_over25"].astype(float), 1.0 / df["pin_under25"].astype(float)
    out["fo"] = io / (io + iu)
    out["fu"] = iu / (io + iu)
    return out


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """historical_odds → una fila por partido con los rasgos previos de cada equipo."""
    if df is None or df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["match_date"] = pd.to_datetime(d["match_date"])
    for c in ("home_goals", "away_goals", "home_shots_target", "away_shots_target"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.sort_values(["match_date", "sport_key"]).reset_index(drop=True)

    teams = d.groupby(["sport_key", "season"]).apply(lambda g: set(g.home) | set(g.away)).to_dict()
    n_teams = {k: len(v) for k, v in teams.items()}
    games = {}                                      # partidos por equipo en la temporada (completa)
    for k, g in d.groupby(["sport_key", "season"]):
        games[k] = int(pd.concat([g.home, g.away]).value_counts().max())

    # el "nombre" (13-nov-26): probabilidad media de ganar de cada equipo por temporada, con Pinnacle y, si
    # falta, con el promedio de las casas (el historial dejó de traer Pinnacle a principios de 2026)
    pinv = d[["pin_home", "pin_draw", "pin_away"]].astype(float).to_numpy()
    avgv = d[["avg_home", "avg_draw", "avg_away"]].astype(float).to_numpy()
    odds = np.where((pinv > 1).all(axis=1)[:, None], pinv, np.where((avgv > 1).all(axis=1)[:, None], avgv, np.nan))
    inv = 1.0 / odds
    tot = inv.sum(axis=1)
    acc = {}
    for lg, se, home, away, ph, pa in zip(d.sport_key, d.season, d.home, d.away, inv[:, 0] / tot, inv[:, 2] / tot):
        if np.isfinite(ph):
            for team, prob in ((home, ph), (away, pa)):
                x = acc.setdefault((lg, se, team), [0.0, 0])
                x[0] += prob
                x[1] += 1
    big = {}                                        # (liga, temporada) → sus grandes de nombre
    for (lg, se), here in teams.items():
        ranked = []
        for team in here:                           # solo los que siguen en la liga esa temporada
            prev = [acc.get((lg, se - k, team)) for k in range(1, REP_SEASONS + 1)]
            if all(x and x[1] >= REP_MIN_GAMES for x in prev):
                ranked.append((sum(x[0] / x[1] for x in prev) / REP_SEASONS, team))
        if len(ranked) >= MIN_LEAGUE_TEAMS:
            big[(lg, se)] = {t for _, t in sorted(ranked, reverse=True)[:TOP_N]}

    hist, table = {}, {}
    shots = {}                                      # (liga, equipo) → goles de más contra sus tiros a puerta
    points = {}                                     # (liga, equipo) → puntos de cada partido (cruza temporadas)
    rows = []
    for r in d.itertuples(index=False):
        k = (r.sport_key, r.season)
        prev = teams.get((r.sport_key, r.season - 1))
        tbl = table.setdefault(k, {})
        ranked = sorted(tbl, key=lambda t: (tbl[t][0], tbl[t][1]))   # peor primero
        feat = {}
        for side, team in (("h", r.home), ("a", r.away)):
            h = hist.get((k, team), [])
            last5 = h[-5:]
            feat[f"{side}_played"] = len(h)
            feat[f"{side}_pts5"] = sum(x[0] for x in last5) if len(last5) == 5 else np.nan
            feat[f"{side}_scored5"] = sum(x[1] > 0 for x in last5) if len(last5) == 5 else np.nan
            feat[f"{side}_conceded5"] = sum(x[2] > 0 for x in last5) if len(last5) == 5 else np.nan
            feat[f"{side}_gf5"] = sum(x[1] for x in last5) if len(last5) == 5 else np.nan     # goles a favor
            feat[f"{side}_ga5"] = sum(x[2] for x in last5) if len(last5) == 5 else np.nan     # goles en contra
            feat[f"{side}_rest"] = (r.match_date - h[-1][5]).days if h else np.nan
            feat[f"{side}_last"] = (h[-1][1] - h[-1][2]) if h else np.nan
            last6 = h[-6:]
            feat[f"{side}_luck6"] = (sum((x[1] - x[2]) - LUCK_SHOT_GOALS * (x[3] - x[4]) for x in last6)
                                     if len(last6) == 6 and all(np.isfinite(x[3]) and np.isfinite(x[4])
                                                                for x in last6) else np.nan)
            last10 = shots.get((r.sport_key, team), [])[-LUCK_GAMES:]
            feat[f"{side}_luck10"] = sum(last10) / len(last10) if len(last10) >= LUCK_MIN else np.nan
            bigs = big.get(k)
            feat[f"{side}_big"] = (1.0 if team in bigs else 0.0) if bigs is not None else np.nan
            form = points.get((r.sport_key, team), [])[-FORM_GAMES:]
            feat[f"{side}_pts10"] = float(sum(form)) if len(form) == FORM_GAMES else np.nan
            feat[f"{side}_promoted"] = bool(prev) and team not in prev
            feat[f"{side}_late"] = len(h) >= games.get(k, 0) - 6 and len(h) >= 10
            pos = ranked.index(team) if team in tbl else None
            feat[f"{side}_bottom3"] = pos is not None and len(tbl) >= n_teams.get(k, 99) - 1 and pos < 3
        rows.append(feat)
        hg, ag = r.home_goals, r.away_goals
        hp = 3 if hg > ag else 1 if hg == ag else 0
        ap = 3 if ag > hg else 1 if hg == ag else 0
        hst, ast = r.home_shots_target, r.away_shots_target
        hist.setdefault((k, r.home), []).append((hp, hg, ag, hst, ast, r.match_date))
        hist.setdefault((k, r.away), []).append((ap, ag, hg, ast, hst, r.match_date))
        points.setdefault((r.sport_key, r.home), []).append(hp)
        points.setdefault((r.sport_key, r.away), []).append(ap)
        if np.isfinite(hst) and np.isfinite(ast):
            over = (hg - ag) - LUCK_K * (hst - ast)
            shots.setdefault((r.sport_key, r.home), []).append(over)
            shots.setdefault((r.sport_key, r.away), []).append(-over)
        for team, pts, gd in ((r.home, hp, hg - ag), (r.away, ap, ag - hg)):
            p, g = tbl.get(team, (0, 0))
            tbl[team] = (p + pts, g + gd)
    f = pd.concat([d, pd.DataFrame(rows), _fair(d)], axis=1)
    f["period"] = None
    for label, lo, hi in PERIODS:
        f.loc[f["season"].between(lo, hi), "period"] = label
    return f


# ─────────────────────────────────────────────────────────────────────────────
# Evaluación
# ─────────────────────────────────────────────────────────────────────────────

def _stats(sel: pd.DataFrame, side: str) -> dict:
    col, fair, won = SIDES[side]
    s = sel[sel[f"avg_{col}"].notna()]
    if s.empty:
        return {"n": 0}
    win = won(s).to_numpy()
    avg = s[f"avg_{col}"].astype(float).to_numpy()
    profit = np.where(win, avg - 1.0, -1.0)
    pin = s[f"pin_{col}"].astype(float).to_numpy()
    has_pin = np.isfinite(pin)
    roi_pin = (float(np.where(win, pin - 1.0, -1.0)[has_pin].mean()) if has_pin.any() else None)
    n = len(profit)
    return {"n": int(n), "hit": float(win.mean()),
            "expected": float(s[fair].mean()) if s[fair].notna().any() else None,
            "roi": float(profit.mean()), "se": float(profit.std(ddof=1) / np.sqrt(n)) if n > 1 else None,
            "roi_pin": roi_pin}


def evaluate(f: pd.DataFrame, hypotheses=HYPOTHESES, born_here=FROM_THIS_HISTORY) -> dict:
    """Cada idea por periodo y junta, con su veredicto (función pura).
    born_here: las ideas nacidas de este historial; a lo más, prometedoras."""
    k = len(hypotheses)
    z_needed = NormalDist().inv_cdf(1 - ALPHA / max(k, 1))
    out = {}
    for key, name, side, rule in hypotheses:
        mask = rule(f).fillna(False).astype(bool) & f["period"].notna()
        sel = f[mask]
        per = {label: _stats(sel[sel["period"] == label], side) for label, _, _ in PERIODS}
        pooled = _stats(sel, side)
        z = (pooled["roi"] / pooled["se"]) if pooled.get("se") else None
        all_n = all(p.get("n", 0) >= MIN_N_PERIOD for p in per.values())
        all_up = all_n and all(p["roi"] > 0 for p in per.values())
        if not all_n:
            v = THIN
        elif all_up and z is not None and z >= z_needed:
            v = PROMISING if key in born_here else PASSES
        elif all_up:
            v = PROMISING
        else:
            v = DISCARDED
        out[key] = {"name": name, "side": side, "periods": per, "pooled": pooled,
                    "z": None if z is None else float(z), "verdict": v, "born_here": key in born_here}
    return {"hypotheses": out, "z_needed": float(z_needed), "k": k}


def build_report(df: pd.DataFrame) -> dict:
    f = build_features(df)
    if f.empty:
        return {"n_matches": 0, "hypotheses": {}, "passed": [], "promising": [],
                "updated_at": datetime.now(timezone.utc).isoformat()}
    ev = evaluate(f)
    hyps = ev["hypotheses"]
    last_pin, last_any = f.loc[f["fh"].notna(), "match_date"].max(), f["match_date"].max()
    return {
        # hasta cuándo trae el historial el precio de Pinnacle (13-nov-26: la fuente dejó de publicarlo a
        # principios de 2026 y nadie lo había notado; las ideas que dependen de él ya no suman partidos)
        "pinnacle_until": None if pd.isna(last_pin) else str(pd.Timestamp(last_pin).date()),
        "history_until": None if pd.isna(last_any) else str(pd.Timestamp(last_any).date()),
        "n_matches": int(len(f)),
        "n_leagues": int(f["sport_key"].nunique()),
        "seasons": [int(f["season"].min()), int(f["season"].max())],
        "periods": [p[0] for p in PERIODS],
        "price": "promedio entre casas (cierre cuando existe)",
        "k": ev["k"], "z_needed": ev["z_needed"],
        "hypotheses": hyps,
        "passed": [k for k, h in hyps.items() if h["verdict"] == PASSES],
        "promising": [k for k, h in hyps.items() if h["verdict"] == PROMISING],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def changed(previous: dict | None, report: dict) -> bool:
    prev = previous or {}
    return (not prev or set(prev.get("passed") or []) != set(report.get("passed") or [])
            or set(prev.get("promising") or []) != set(report.get("promising") or []))


# ─────────────────────────────────────────────────────────────────────────────
# DB y reporte
# ─────────────────────────────────────────────────────────────────────────────

def read_history() -> pd.DataFrame:
    from config.database import engine
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('historical_odds')")).scalar() is None:
            return pd.DataFrame()
    return pd.read_sql(text("""
        SELECT sport_key, season, match_date, home, away, home_goals, away_goals,
               home_shots_target, away_shots_target,
               pin_home, pin_draw, pin_away, avg_home, avg_draw, avg_away,
               pin_over25, pin_under25, avg_over25, avg_under25
        FROM historical_odds
    """), engine)


def run_edge_finder(verbose: bool = True) -> dict:
    """Paso semanal: prueba todas las ideas, guarda en model_state y devuelve."""
    from src.utils.model_state import save_state
    report = build_report(read_history())
    save_state(STATE_KEY, report)
    if verbose:
        print(format_report(report, html=False))
    return report


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def format_report(rep: dict, html: bool = True) -> str:
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    hyps = rep.get("hypotheses") or {}
    if not rep.get("n_matches"):
        return b("🔎 BUSCADOR DE VENTAJAS") + "\nTodavía no hay momios históricos cargados."
    lines = [b("🔎 BUSCADOR DE VENTAJAS"),
             f"{rep['k']} ideas probadas en {rep['n_matches']:,} partidos de {rep['n_leagues']} ligas "
             f"({rep['seasons'][0]}-{rep['seasons'][1]}), al precio de una casa común."]

    until, end = rep.get("pinnacle_until"), rep.get("history_until")
    if until and end and (pd.Timestamp(end) - pd.Timestamp(until)).days > PINNACLE_GAP_DAYS:
        lines.append(f"⚠️ El historial trae el precio de Pinnacle solo hasta {until}: lo jugado después (hasta {end}) "
                     f"no entra a las ideas que dependen de ese precio.")

    def line(key):
        h = hyps[key]
        per = " · ".join(f"{lbl} {_pct(p.get('roi'))}" for lbl, p in h["periods"].items())
        note = ""
        if h.get("born_here") and h["verdict"] == PROMISING and (h.get("z") or 0) >= (rep.get("z_needed") or 9e9):
            note = (" — pasaría la prueba, pero la idea salió de mirar este mismo historial: falta verla en "
                    "partidos nuevos")
        return f"  • {h['name']}: {_pct(h['pooled'].get('roi'))} (n={h['pooled'].get('n', 0):,}; {per}){note}"

    passed, promising = rep.get("passed") or [], rep.get("promising") or []
    lines.append("✅ Pasaron las 3 pruebas: " + ("" if passed else "ninguna todavía."))
    lines += [line(k) for k in passed]
    if promising:
        lines.append("🟡 Prometedoras (ganan en los 3 periodos, pero aún puede ser suerte):")
        lines += [line(k) for k in promising]
    discarded = [k for k, h in hyps.items() if h["verdict"] == DISCARDED]
    lines.append(f"❌ Descartadas: {len(discarded)}. Por ejemplo:")
    for k in ("local_racha", "ambos_goleadores", "alguno_goleable"):
        if k in hyps and hyps[k]["verdict"] == DISCARDED:
            h = hyps[k]
            lines.append(f"  • {h['name']}: gana {h['pooled']['hit'] * 100:.0f}%, la casa esperaba "
                         f"{(h['pooled'].get('expected') or 0) * 100:.0f}% → {_pct(h['pooled']['roi'])}")
    lines.append(f"Una idea pasa solo si gana en {', '.join(rep['periods'])} y la ganancia no puede ser "
                 f"suerte aun probando {rep['k']} ideas.")
    return "\n".join(lines)
