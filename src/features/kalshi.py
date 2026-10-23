"""
src/features/kalshi.py
======================
Precios de Kalshi como segunda referencia sin margen (14-oct-26).

Kalshi es un mercado de predicciones regulado en EE. UU.: el precio de cada
contrato (0 a 1 dólar) es la probabilidad, sin margen de casa. Medido el
14-oct-26 en 104 partidos: su 1X2 difiere de Pinnacle 0.75 pt en promedio
(correlación 0.998), con 1.7 centavos entre compra y venta. En 1X2 no aporta
información nueva; sirve en lo que Pinnacle NO publica en nuestro feed:
ambos anotan y ganador del primer tiempo (más el over/under 2.5 de respaldo).
Así las candidatas "sin referencia" (casi la mitad) se pueden medir contra un
cierre justo. Córners y tarjetas no: allí Kalshi casi no opera (25-46 c de
diferencia).

SOLO MIDE. API pública de lectura: sin clave y sin créditos de The Odds API.
Se guarda en kalshi_history solo cuando el precio cambió (como odds_history).

Emparejamiento (23-oct-26): sumaba el parecido de los dos nombres y una
palabra larga compartida contaba como nombre igual; cuando el partido
verdadero ya no estaba en la ventana, el de Gimnasia Mendoza vs Platense se
guardó como Gimnasia LP vs Sarmiento ("gimnasia" + Platense~Sarmiento 0.26).
Ahora cada nombre tiene que parecerse por separado (KALSHI_MIN_SIDE; Kalshi
abrevia mucho, por eso más bajo que el de ESPN), las palabras genéricas no
bastan, y un 1X2 de Kalshi a más de MAX_PIN_GAP de Pinnacle no es ese partido
(se descarta todo lo de ese evento): Kalshi y Pinnacle difieren 0.74 pt en
promedio (354 partidos).
"""

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from src.utils.log import get_logger
from src.utils.team_normalizer import normalize_team

log = get_logger(__name__)

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# sport_key del sistema → código de liga en los tickers de Kalshi (KX<código><serie>)
LEAGUE_CODES = {
    "soccer_epl": "EPL",
    "soccer_efl_champ": "EFLCHAMPIONSHIP",
    "soccer_spain_la_liga": "LALIGA",
    "soccer_germany_bundesliga": "BUNDESLIGA",
    "soccer_italy_serie_a": "SERIEA",
    "soccer_france_ligue_one": "LIGUE1",
    "soccer_netherlands_eredivisie": "EREDIVISIE",
    "soccer_portugal_primeira_liga": "LIGAPORTUGAL",
    "soccer_belgium_first_div": "BELGIANPL",
    "soccer_turkey_super_league": "SUPERLIG",
    "soccer_greece_super_league": "SLGREECE",
    "soccer_spl": "SCOTTISHPREM",
    "soccer_usa_mls": "MLS",
    "soccer_mexico_ligamx": "LIGAMX",
    "soccer_brazil_campeonato": "BRASILEIRO",
    "soccer_argentina_primera_division": "ARGPREMDIV",
    "soccer_korea_kleague1": "KLEAGUE",
    "soccer_japan_j_league": "JLEAGUE",
    "soccer_norway_eliteserien": "ELITESERIEN",
    "soccer_sweden_allsvenskan": "ALLSVENSKAN",
    "soccer_uefa_champs_league": "UCL",
    "soccer_uefa_europa_league": "UEL",
}
SERIES = ("GAME", "BTTS", "1H", "TOTAL")
MAX_SPREAD = 0.08        # más de 8 centavos entre compra y venta no es un precio
# Kalshi publica el FIN estimado, no el inicio, y el desfase depende de la
# serie (medido el 14-oct-26 en Premier, La Liga y Champions: ganador y 1T
# +3 h, ambos anotan y totales +4 h). Es solo una estimación para emparejar:
# se guarda la hora de inicio del sistema, no esta.
END_OFFSET_H = {"GAME": 3, "1H": 3, "BTTS": 4, "TOTAL": 4}
MATCH_WINDOW_S = 90 * 60
KALSHI_MIN_SIDE = 0.5    # parecido mínimo de CADA nombre (Kalshi abrevia: "m gladbach", "bilbao")
MAX_PIN_GAP = 0.10       # 1X2 de Kalshi contra el de Pinnacle: más lejos = otro partido

KALSHI_DDL = """
    CREATE TABLE IF NOT EXISTS kalshi_history (
        id          BIGSERIAL PRIMARY KEY,
        captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        league      TEXT NOT NULL,
        home        TEXT NOT NULL,
        away        TEXT NOT NULL,
        kickoff     TIMESTAMPTZ NOT NULL,
        market      TEXT NOT NULL,
        prob        NUMERIC NOT NULL,
        spread      NUMERIC,
        volume      NUMERIC
    )
"""
KALSHI_INDEX = """
    CREATE INDEX IF NOT EXISTS idx_kalshi_history_key
        ON kalshi_history (home, away, kickoff, market, captured_at DESC)
"""


RETRY_STATUS = (429, 500, 502, 503, 504)
PAUSE_S = 0.25          # entre llamadas: ~4 por segundo


def _get(path: str, tries: int = 4, sleep=None) -> dict:
    """GET a la API de Kalshi con reintento. Desde GitHub, 5-9 ligas por corrida
    fallaban con HTTPError (20-oct-26) — las mismas respondían bien desde otra
    máquina: el límite de llamadas de Kalshi con ~90 seguidas. Se espera
    1, 2, 4 s ante 429/5xx y se hace una pausa corta entre llamadas."""
    import time
    sleep = sleep or time.sleep
    for attempt in range(tries):
        sleep(PAUSE_S)
        req = urllib.request.Request(BASE + path, headers={"User-Agent": "sport-betting-model"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or attempt == tries - 1:
                raise
            sleep(2 ** attempt)


def _quote(m: dict):
    """(precio medio, diferencia compra-venta, volumen) o None sin precio real."""
    try:
        bid = float(m.get("yes_bid_dollars") or 0)
        ask = float(m.get("yes_ask_dollars") or 0)
    except (TypeError, ValueError):
        return None
    if not (0 < bid < ask <= 1) or ask - bid > MAX_SPREAD:
        return None
    return (bid + ask) / 2, ask - bid, float(m.get("volume_fp") or 0)


def _teams(title: str):
    """'Everton vs Chelsea: BTTS' → ('everton', 'chelsea') normalizados."""
    head = str(title or "").split(":")[0]
    if " vs " not in head:
        return None
    h, a = head.split(" vs ", 1)
    return normalize_team(h.strip()), normalize_team(a.strip())


def parse_event(series: str, league: str, event: dict) -> list:
    """
    Filas {league, home, away, kickoff, market, prob, spread, volume} con los
    nombres de mercado del sistema (función pura). 1X2 y primer tiempo se
    normalizan a sumar 1; ambos anotan y over 2.5 dan su contrario.
    """
    teams = _teams(event.get("title"))
    markets = event.get("markets") or []
    if not teams or not markets:
        return []
    home, away = teams
    end = markets[0].get("occurrence_datetime") or markets[0].get("expected_expiration_time")
    if not end:
        return []
    kickoff = (datetime.fromisoformat(str(end).replace("Z", "+00:00"))
               - timedelta(hours=END_OFFSET_H.get(series, 3))).isoformat()
    base = {"league": league, "home": home, "away": away, "kickoff": kickoff}
    out = []

    if series in ("GAME", "1H"):
        names = ({"h": "home_win", "d": "draw", "a": "away_win"} if series == "GAME"
                 else {"h": "h1_home", "d": "h1_draw", "a": "h1_away"})
        q = {}
        for m in markets:
            quote = _quote(m)
            sub = str(m.get("yes_sub_title") or m.get("title") or "")
            ticker = str(m.get("ticker") or "")
            if quote is None:
                return []                       # sin los tres precios no hay 1X2
            if ticker.endswith("-TIE") or sub.lower().startswith("tie"):
                q["d"] = quote
            else:
                team = normalize_team(sub.replace("wins 1st Half", "").strip())
                if team == home:
                    q["h"] = quote
                elif team == away:
                    q["a"] = quote
        if set(q) != {"h", "d", "a"}:
            return []
        total = sum(v[0] for v in q.values())
        for k, (p, sp, vol) in q.items():
            out.append({**base, "market": names[k], "prob": p / total, "spread": sp, "volume": vol})
    elif series == "BTTS":
        quote = _quote(markets[0])
        if quote:
            p, sp, vol = quote
            out += [{**base, "market": "btts", "prob": p, "spread": sp, "volume": vol},
                    {**base, "market": "btts_no", "prob": 1 - p, "spread": sp, "volume": vol}]
    elif series == "TOTAL":
        for m in markets:
            if m.get("floor_strike") in (2.5, "2.5"):
                quote = _quote(m)
                if quote:
                    p, sp, vol = quote
                    out += [{**base, "market": "over25", "prob": p, "spread": sp, "volume": vol},
                            {**base, "market": "under25", "prob": 1 - p, "spread": sp, "volume": vol}]
    return out


def _name_score(a: str, b: str) -> float:
    """Parecido de dos nombres de equipo (0-1). Kalshi abrevia ("bilbao",
    "frankfurt", "m gladbach"): una palabra larga contenida en el otro nombre
    cuenta como igual."""
    import difflib
    from src.features.espn_lineups import GENERIC_WORDS
    if a == b:
        return 1.0
    own = (lambda x, y: any(len(w) >= 5 and w not in GENERIC_WORDS and w in y for w in x.split()))
    if own(a, b) or own(b, a):
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def match_fixture(event: dict, fixtures) -> tuple | None:
    """
    (local, visitante, inicio) del partido del sistema que corresponde a un
    evento de Kalshi, o None (función pura). fixtures: [(liga, local,
    visitante, inicio)]. Misma liga, inicio estimado a ±90 min, cada nombre
    parecido por separado (KALSHI_MIN_SIDE) y el mejor de los dos; si dos
    partidos se parecen casi igual, no se adivina.
    """
    ko = event["kick"]
    cands = []
    seen = set()          # upcoming_matches puede tener la misma ficha dos veces
    for lg, h, a, k in fixtures:
        if lg != event["league"] or abs((k - ko).total_seconds()) > MATCH_WINDOW_S or (h, a) in seen:
            continue
        seen.add((h, a))
        sh, sa = _name_score(event["home"], h), _name_score(event["away"], a)
        if min(sh, sa) >= KALSHI_MIN_SIDE:
            cands.append((sh + sa, h, a, k))
    if not cands:
        return None
    cands.sort(reverse=True)
    best = cands[0]
    if best[0] < 1.2 or (len(cands) > 1 and best[0] - cands[1][0] < 0.3):
        return None
    return best[1], best[2], best[3]


def price_mismatches(rows: list, pinnacle: dict) -> set:
    """Función pura: las claves (liga, local, visitante, inicio ISO) cuyo 1X2 de
    Kalshi está a más de MAX_PIN_GAP del de Pinnacle (pinnacle: misma clave →
    (local, empate, visitante) sin margen). Sin 1X2 o sin Pinnacle no se juzga."""
    game = {}
    for r in rows:
        if r["market"] in ("home_win", "away_win"):
            game.setdefault((r["league"], r["home"], r["away"], r["kickoff"]), {})[r["market"]] = r["prob"]
    out = set()
    for key, g in game.items():
        pin = pinnacle.get(key)
        if pin is None or len(g) < 2:
            continue
        if abs(g["home_win"] - pin[0]) > MAX_PIN_GAP or abs(g["away_win"] - pin[2]) > MAX_PIN_GAP:
            out.add(key)
    return out


def _pinnacle_1x2(conn, horizon_hours: float) -> dict:
    """{(liga, local, visitante, inicio ISO): 1X2 de Pinnacle sin margen} de upcoming_matches."""
    from src.features.market_odds import market_probabilities
    out = {}
    for lg, h, a, k, ph, pd_, pa in conn.execute(text("""
        SELECT DISTINCT ON (sport_key, home_team_norm, away_team_norm, match_date)
               sport_key, home_team_norm, away_team_norm, match_date, pin_home_odds, pin_draw_odds, pin_away_odds
        FROM upcoming_matches
        WHERE match_date BETWEEN NOW() AND NOW() + make_interval(hours => :h) AND pin_home_odds IS NOT NULL
        ORDER BY sport_key, home_team_norm, away_team_norm, match_date, updated_at DESC NULLS LAST
    """), {"h": int(horizon_hours)}).fetchall():
        try:
            p = market_probabilities(float(ph), float(pd_), float(pa))
        except (TypeError, ValueError):
            continue
        if p and p[0] is not None:
            out[(lg, h, a, k.isoformat())] = tuple(float(x) for x in p)
    return out


def fetch_league(sport_key: str) -> list:
    code = LEAGUE_CODES.get(sport_key)
    if not code:
        return []
    rows = []
    for series in SERIES:
        cursor = ""
        for _ in range(5):                         # 200 eventos por página
            data = _get(f"/events?series_ticker=KX{code}{series}&status=open"
                        f"&with_nested_markets=true&limit=200" + (f"&cursor={cursor}" if cursor else ""))
            for ev in data.get("events") or []:
                rows += parse_event(series, sport_key, ev)
            cursor = data.get("cursor") or ""
            if not cursor:
                break
    return rows


def capture_kalshi(horizon_hours: float = 96, every: bool = False, verbose: bool = True) -> int:
    """
    Guarda los precios de Kalshi de los partidos de las ligas con partidos en
    las próximas `horizon_hours` horas. Morning y evening (horizonte largo,
    la apertura) guardan solo lo que cambió; el closing (horizonte corto,
    every=True) guarda todo, porque el cierre es la última foto antes del
    kickoff y un precio que no cambió también es cierre.
    """
    from config.database import engine
    now = datetime.now(timezone.utc)
    with engine.connect() as conn:
        fixtures = [(r[0], r[1], r[2], r[3]) for r in conn.execute(text("""
            SELECT DISTINCT sport_key, home_team_norm, away_team_norm, match_date
            FROM upcoming_matches
            WHERE match_date BETWEEN NOW() AND NOW() + make_interval(hours => :h)
        """), {"h": int(horizon_hours)}).fetchall()]
    leagues = {f[0] for f in fixtures}
    rows, failed = [], []
    for lg in sorted(set(leagues) & set(LEAGUE_CODES)):
        try:
            rows += fetch_league(lg)
        except Exception as e:
            failed.append(f"{lg}: {type(e).__name__} {getattr(e, 'code', '')}".strip())
    limit = now + timedelta(hours=horizon_hours)
    seen = len(rows)
    matched, cache = [], {}
    for r in rows:
        if not r["kickoff"]:
            continue
        r["kick"] = datetime.fromisoformat(str(r["kickoff"]).replace("Z", "+00:00"))
        if not (now < r["kick"] <= limit):
            continue
        key = (r["league"], r["home"], r["away"], r["kick"])
        if key not in cache:
            cache[key] = match_fixture(r, fixtures)
        if cache[key]:
            # se guarda con los nombres y la hora del sistema, no los de Kalshi
            r["home"], r["away"], kick = cache[key]
            r["kickoff"] = kick.isoformat()
            matched.append(r)
    rows = matched
    if rows:
        with engine.connect() as conn:
            pin = _pinnacle_1x2(conn, horizon_hours)
        bad = price_mismatches(rows, pin)
        if bad:
            log.warning(f"⚠️  Kalshi: {len(bad)} evento(s) con un 1X2 lejos de Pinnacle — emparejados con otro "
                        f"partido, no se guardan: {sorted(f'{k[1]} vs {k[2]}' for k in bad)[:3]}")
            rows = [r for r in rows if (r["league"], r["home"], r["away"], r["kickoff"]) not in bad]
    inserted = 0
    if rows:
        with engine.begin() as conn:
            conn.execute(text(KALSHI_DDL))
            conn.execute(text(KALSHI_INDEX))
            for i in range(0, len(rows), 300):
                part = rows[i:i + 300]
                vals, params = [], {}
                for k, r in enumerate(part):
                    vals.append(f"(:l{k}, :h{k}, :a{k}, CAST(:k{k} AS timestamptz), :m{k}, "
                                f"CAST(:p{k} AS numeric), CAST(:s{k} AS numeric), CAST(:v{k} AS numeric))")
                    params.update({f"l{k}": r["league"], f"h{k}": r["home"], f"a{k}": r["away"],
                                   f"k{k}": r["kickoff"], f"m{k}": r["market"],
                                   f"p{k}": round(r["prob"], 4), f"s{k}": round(r["spread"], 4),
                                   f"v{k}": r["volume"]})
                res = conn.execute(text(f"""
                    INSERT INTO kalshi_history (league, home, away, kickoff, market, prob, spread, volume)
                    SELECT v.* FROM (VALUES {", ".join(vals)})
                        AS v(league, home, away, kickoff, market, prob, spread, volume)
                    LEFT JOIN LATERAL (
                        SELECT h.prob FROM kalshi_history h
                        WHERE h.home = v.home AND h.away = v.away AND h.kickoff = v.kickoff
                          AND h.market = v.market
                        ORDER BY h.captured_at DESC LIMIT 1
                    ) prev ON TRUE
                    WHERE :every OR prev.prob IS NULL OR prev.prob <> v.prob
                """), {**params, "every": bool(every)})
                inserted += res.rowcount or 0
    if failed:
        # WARNING: Kalshi solo mide; un tropiezo de su API no debe teñir la corrida
        log.warning(f"⚠️  Kalshi sin respuesta en {len(failed)} liga(s): {'; '.join(failed[:4])}")
    if verbose:
        print(f"📈 Kalshi: {seen} precios vistos, {len(rows)} de partidos del sistema, "
              f"{inserted} cambios guardados ({len(set(leagues) & set(LEAGUE_CODES))} ligas)")
    return inserted


def stored_mismatches(engine, days: int = 7) -> list:
    """Partidos de kalshi_history de los últimos `days` días cuyo 1X2 está a más
    de MAX_PIN_GAP de Pinnacle (la última foto de odds_history): emparejados con
    otro partido. [(liga, local, visitante, inicio, kalshi local, pinnacle local)]."""
    from src.features.market_odds import market_probabilities
    with engine.connect() as conn:
        if conn.execute(text("SELECT to_regclass('kalshi_history')")).scalar() is None:
            return []
        k = conn.execute(text("""
            SELECT DISTINCT ON (league, home, away, kickoff, market) league, home, away, kickoff, market, prob
            FROM kalshi_history WHERE market IN ('home_win', 'away_win')
              AND kickoff >= NOW() - make_interval(days => :d)
            ORDER BY league, home, away, kickoff, market, captured_at DESC"""), {"d": days}).fetchall()
        pin = conn.execute(text("""
            SELECT DISTINCT ON (league, match_key, kickoff) league, match_key, kickoff,
                   pin_home_odds, pin_draw_odds, pin_away_odds
            FROM odds_history WHERE pin_home_odds IS NOT NULL AND kickoff >= NOW() - make_interval(days => :d)
            ORDER BY league, match_key, kickoff, captured_at DESC"""), {"d": days + 1}).fetchall()
    fair = {}
    for lg, mk, kick, ph, pdr, pa in pin:
        p = market_probabilities(float(ph), float(pdr), float(pa))
        if p and p[0] is not None:
            fair[(lg, str(mk).rsplit("_", 1)[0], kick)] = p        # match_key = local_visitante_fecha
    game = {}
    for lg, h, a, kick, market, prob in k:
        game.setdefault((lg, h, a, kick), {})[market] = float(prob)
    out = []
    for (lg, h, a, kick), g in game.items():
        p = fair.get((lg, f"{h}_{a}", kick))
        if p is None or len(g) < 2:
            continue
        if abs(g["home_win"] - p[0]) > MAX_PIN_GAP or abs(g["away_win"] - p[2]) > MAX_PIN_GAP:
            out.append((lg, h, a, kick, g["home_win"], float(p[0])))
    return out
