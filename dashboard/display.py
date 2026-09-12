"""
dashboard/display.py
====================
Capa de presentación: convierte nombres internos a etiquetas formales.

- Ligas:  "soccer_efl_champ" → "EFL Championship"
- Mercados: "home_win" → "1 · Local", "corners_over_9.5" → "Córners +9.5"
- Equipos: "nottm forest" → "Nottingham Forest"

SOLO afecta lo que se muestra en el dashboard — la DB y el pipeline
siguen usando los nombres normalizados (así el matching no se rompe).
"""

import re

LEAGUE_NAMES = {
    "soccer_epl": "Premier League",
    "soccer_efl_champ": "EFL Championship",
    "soccer_spain_la_liga": "LaLiga",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_france_ligue_one": "Ligue 1",
    "soccer_netherlands_eredivisie": "Eredivisie",
    "soccer_portugal_primeira_liga": "Primeira Liga",
    "soccer_belgium_first_div": "Pro League (Bélgica)",
    "soccer_greece_super_league": "Super League (Grecia)",
    "soccer_turkey_super_league": "Süper Lig (Turquía)",
    "soccer_spl": "Scottish Premiership",
    "soccer_sweden_allsvenskan": "Allsvenskan",
    "soccer_norway_eliteserien": "Eliteserien",
    "soccer_mexico_ligamx": "Liga MX",
    "soccer_brazil_campeonato": "Brasileirão",
    "soccer_argentina_primera_division": "Primera División (Argentina)",
    "soccer_usa_mls": "MLS",
    "soccer_china_superleague": "Super League (China)",
    "soccer_japan_j_league": "J1 League",
    "soccer_korea_kleague1": "K League 1",
    "soccer_uefa_champs_league": "Champions League",
    "soccer_uefa_europa_league": "Europa League",
    "soccer_conmebol_copa_libertadores": "Copa Libertadores",
    "soccer_fifa_world_cup": "Mundial FIFA",
}

MARKET_NAMES = {
    "home_win": "1 · Local",
    "draw": "X · Empate",
    "away_win": "2 · Visitante",
    "over25": "Más 2.5 goles",
    "under25": "Menos 2.5 goles",
    "btts": "Ambos anotan",
    "btts_yes": "Ambos anotan · Sí",
    "btts_no": "Ambos anotan · No",
    "dnb_home": "Empate anulado · Local",
    "dnb_away": "Empate anulado · Visitante",
    "dc_1x": "Doble oportunidad 1X",
    "dc_x2": "Doble oportunidad X2",
    "dc_12": "Doble oportunidad 12",
}

# Abreviaturas de equipos → nombre oficial (display solamente)
TEAM_DISPLAY = {
    "nottm forest": "Nottingham Forest",
    "man united": "Manchester United",
    "man city": "Manchester City",
    "wolves": "Wolverhampton",
    "spurs": "Tottenham",
    "psg": "Paris Saint-Germain",
    "inter miami": "Inter Miami CF",
    "internacional": "Sport Club Internacional",
    "ath madrid": "Atlético de Madrid",
    "atletico madrid": "Atlético de Madrid",
    "sporting lisbon": "Sporting CP",
    "sporting cp": "Sporting CP",
    "bayern munchen": "Bayern Múnich",
    "bayern munich": "Bayern Múnich",
    "boca juniors": "Boca Juniors",
    "river plate": "River Plate",
    "america": "Club América",
    "cruz azul": "Cruz Azul",
    "chivas guadalajara": "Chivas Guadalajara",
    "guadalajara": "Chivas Guadalajara",
    "barcelona sc": "Barcelona SC",
}

# Partículas que van en mayúsculas / minúsculas al aplicar título
_UPPER_WORDS = {"fc", "ac", "sc", "cf", "if", "bk", "aif", "afc", "u19", "u23", "rj", "sp", "mg", "go"}
_LOWER_WORDS = {"de", "del", "la", "las", "los", "y", "el"}


def league_name(key: str) -> str:
    if not key:
        return ""
    if key in LEAGUE_NAMES:
        return LEAGUE_NAMES[key]
    # fallback: quitar prefijo y capitalizar
    clean = re.sub(r"^(soccer|baseball)_", "", str(key)).replace("_", " ")
    return clean.title()


def team_name(name: str) -> str:
    if not name:
        return ""
    n = str(name).strip().lower()
    if n in TEAM_DISPLAY:
        return TEAM_DISPLAY[n]
    words = []
    for w in n.split():
        if w in _UPPER_WORDS:
            words.append(w.upper())
        elif w in _LOWER_WORDS:
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def market_name(key: str) -> str:
    if not key:
        return ""
    k = str(key)
    if k in MARKET_NAMES:
        return MARKET_NAMES[k]

    # corners_over_9.5 → Córners +9.5 | cards_under_4.5 → Tarjetas −4.5
    m = re.match(r"^corners_(over|under)_([\d.]+)$", k)
    if m:
        return f"Córners {'+' if m.group(1) == 'over' else '−'}{m.group(2)}"
    m = re.match(r"^cards_(over|under)_([\d.]+)$", k)
    if m:
        return f"Tarjetas {'+' if m.group(1) == 'over' else '−'}{m.group(2)}"
    m = re.match(r"^shots_(over|under)_([\d.]+)$", k)
    if m:
        return f"Tiros arco {'+' if m.group(1) == 'over' else '−'}{m.group(2)}"

    # ah_home_-0.5 → Hándicap Local −0.5
    m = re.match(r"^ah_(home|away)_([+-]?[\d.]+)$", k)
    if m:
        side = "Local" if m.group(1) == "home" else "Visitante"
        return f"Hándicap {side} {m.group(2)}"

    # h1_home → 1er tiempo · Local
    m = re.match(r"^h([12])_(home|draw|away)$", k)
    if m:
        period = "1er tiempo" if m.group(1) == "1" else "2do tiempo"
        side = {"home": "Local", "draw": "Empate", "away": "Visitante"}[m.group(2)]
        return f"{period} · {side}"

    # over_1.5 / under_3.5
    m = re.match(r"^(over|under)_([\d.]+)$", k)
    if m:
        return f"{'Más' if m.group(1) == 'over' else 'Menos'} {m.group(2)} goles"

    return k.replace("_", " ").title()


def match_name(match: str) -> str:
    """'nottm forest vs aston villa' → 'Nottingham Forest vs Aston Villa'"""
    if not match or " vs " not in str(match):
        return str(match) if match else ""
    h, a = str(match).split(" vs ", 1)
    return f"{team_name(h)} vs {team_name(a)}"


def result_label(r: str) -> str:
    return {
        "win": "Ganada", "loss": "Perdida", "push": "Nula",
        "half_win": "Media ganada", "half_loss": "Media perdida",
        "pending": "Pendiente",
        "unresolved": "Esperando datos",
        "stale": "Sin fuente",
    }.get(r or "", r or "")
