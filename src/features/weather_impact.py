"""
Weather Impact
==============
Ajusta los lambdas de goles según las condiciones meteorológicas del estadio.

Evidencia:
  - Lluvia intensa:   -6% goles (balón pesado, campo lento)
  - Viento fuerte:    -5% goles (balones largos, corners menos precisos)
  - Nieve / granizo:  -9% goles (campo en mal estado)
  - Frío extremo:     -3% goles (músculos rígidos, ritmo lento)
  - Calor extremo:    -4% goles (fatiga, ritmo reducido)

Fuente: Gómez & Pollard (2011), Havenith et al. (2007)

API: OpenWeatherMap (gratis 1,000 calls/día)
  Registro: https://openweathermap.org/api
  Clave en .env: WEATHER_API_KEY=tu_clave

Nota: si la clave no está configurada o la llamada falla,
      retorna multiplicador 1.0 (sin efecto) para no interrumpir el pipeline.
"""

import requests
from config.settings import WEATHER_API_KEY

WEATHER_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"
WEATHER_TIMEOUT  = 5   # segundos

# ─── Mapa equipo → ciudad del estadio ─────────────────────────────────────
# Usado para obtener el pronóstico del tiempo en el estadio local.
# El equipo VISITANTE juega en la ciudad del equipo LOCAL.

STADIUM_CITY = {
    # ── Premier League ────────────────────────────────────────────────────
    "arsenal":              "London,GB",
    "chelsea":              "London,GB",
    "tottenham":            "London,GB",
    "tottenham hotspur":    "London,GB",
    "west ham":             "London,GB",
    "west ham united":      "London,GB",
    "crystal palace":       "London,GB",
    "brentford":            "London,GB",
    "fulham":               "London,GB",
    "manchester city":      "Manchester,GB",
    "man city":             "Manchester,GB",
    "manchester united":    "Manchester,GB",
    "man united":           "Manchester,GB",
    "liverpool":            "Liverpool,GB",
    "everton":              "Liverpool,GB",
    "newcastle":            "Newcastle upon Tyne,GB",
    "newcastle united":     "Newcastle upon Tyne,GB",
    "aston villa":          "Birmingham,GB",
    "wolverhampton":        "Wolverhampton,GB",
    "wolves":               "Wolverhampton,GB",
    "brighton":             "Brighton,GB",
    "brighton & hove albion": "Brighton,GB",
    "southampton":          "Southampton,GB",
    "bournemouth":          "Bournemouth,GB",
    "leicester":            "Leicester,GB",
    "leicester city":       "Leicester,GB",
    "nottingham forest":    "Nottingham,GB",
    "sheffield united":     "Sheffield,GB",
    "sheffield wednesday":  "Sheffield,GB",
    "burnley":              "Burnley,GB",
    "luton":                "Luton,GB",
    "luton town":           "Luton,GB",
    "ipswich":              "Ipswich,GB",
    "ipswich town":         "Ipswich,GB",
    "sunderland":           "Sunderland,GB",
    "middlesbrough":        "Middlesbrough,GB",
    "leeds":                "Leeds,GB",
    "leeds united":         "Leeds,GB",

    # ── La Liga ───────────────────────────────────────────────────────────
    "real madrid":          "Madrid,ES",
    "barcelona":            "Barcelona,ES",
    "atletico madrid":      "Madrid,ES",
    "atletico de madrid":   "Madrid,ES",
    "sevilla":              "Seville,ES",
    "real betis":           "Seville,ES",
    "villarreal":           "Villarreal,ES",
    "athletic bilbao":      "Bilbao,ES",
    "athletic club":        "Bilbao,ES",
    "real sociedad":        "San Sebastian,ES",
    "valencia":             "Valencia,ES",
    "osasuna":              "Pamplona,ES",
    "ca osasuna":           "Pamplona,ES",
    "celta vigo":           "Vigo,ES",
    "rayo vallecano":       "Madrid,ES",
    "getafe":               "Getafe,ES",
    "girona":               "Girona,ES",
    "mallorca":             "Palma,ES",
    "alaves":               "Vitoria-Gasteiz,ES",
    "deportivo alaves":     "Vitoria-Gasteiz,ES",
    "cadiz":                "Cadiz,ES",
    "almeria":              "Almeria,ES",
    "granada":              "Granada,ES",
    "las palmas":           "Las Palmas,ES",
    "real valladolid":      "Valladolid,ES",
    "espanyol":             "Barcelona,ES",
    "leganes":              "Leganes,ES",

    # ── Bundesliga ────────────────────────────────────────────────────────
    "bayern munich":        "Munich,DE",
    "fc bayern munich":     "Munich,DE",
    "borussia dortmund":    "Dortmund,DE",
    "dortmund":             "Dortmund,DE",
    "rb leipzig":           "Leipzig,DE",
    "bayer leverkusen":     "Leverkusen,DE",
    "eintracht frankfurt":  "Frankfurt,DE",
    "wolfsburg":            "Wolfsburg,DE",
    "vfl wolfsburg":        "Wolfsburg,DE",
    "borussia monchengladbach": "Monchengladbach,DE",
    "monchengladbach":      "Monchengladbach,DE",
    "sc freiburg":          "Freiburg,DE",
    "freiburg":             "Freiburg,DE",
    "union berlin":         "Berlin,DE",
    "1. fc union berlin":   "Berlin,DE",
    "werder bremen":        "Bremen,DE",
    "hoffenheim":           "Sinsheim,DE",
    "tsg hoffenheim":       "Sinsheim,DE",
    "augsburg":             "Augsburg,DE",
    "fc augsburg":          "Augsburg,DE",
    "mainz":                "Mainz,DE",
    "1. fsv mainz 05":      "Mainz,DE",
    "vfb stuttgart":        "Stuttgart,DE",
    "stuttgart":            "Stuttgart,DE",
    "fc heidenheim":        "Heidenheim,DE",
    "holstein kiel":        "Kiel,DE",
    "hamburger sv":         "Hamburg,DE",
    "hamburger":            "Hamburg,DE",
    "schalke":              "Gelsenkirchen,DE",

    # ── Serie A ───────────────────────────────────────────────────────────
    "juventus":             "Turin,IT",
    "inter":                "Milan,IT",
    "inter milan":          "Milan,IT",
    "ac milan":             "Milan,IT",
    "milan":                "Milan,IT",
    "roma":                 "Rome,IT",
    "as roma":              "Rome,IT",
    "lazio":                "Rome,IT",
    "napoli":               "Naples,IT",
    "atalanta":             "Bergamo,IT",
    "fiorentina":           "Florence,IT",
    "torino":               "Turin,IT",
    "sassuolo":             "Sassuolo,IT",
    "udinese":              "Udine,IT",
    "bologna":              "Bologna,IT",
    "verona":               "Verona,IT",
    "hellas verona":        "Verona,IT",
    "sampdoria":            "Genoa,IT",
    "genoa":                "Genoa,IT",
    "cagliari":             "Cagliari,IT",
    "lecce":                "Lecce,IT",
    "empoli":               "Empoli,IT",
    "monza":                "Monza,IT",
    "frosinone":            "Frosinone,IT",
    "salernitana":          "Salerno,IT",
    "venezia":              "Venice,IT",
    "como":                 "Como,IT",
    "parma":                "Parma,IT",

    # ── Ligue 1 ───────────────────────────────────────────────────────────
    "psg":                  "Paris,FR",
    "paris saint-germain":  "Paris,FR",
    "paris sg":             "Paris,FR",
    "marseille":            "Marseille,FR",
    "olympique marseille":  "Marseille,FR",
    "lyon":                 "Lyon,FR",
    "olympique lyonnais":   "Lyon,FR",
    "monaco":               "Monaco,MC",
    "as monaco":            "Monaco,MC",
    "lille":                "Lille,FR",
    "rennes":               "Rennes,FR",
    "stade rennais":        "Rennes,FR",
    "nice":                 "Nice,FR",
    "ogc nice":             "Nice,FR",
    "lens":                 "Lens,FR",
    "rc lens":              "Lens,FR",
    "strasbourg":           "Strasbourg,FR",
    "nantes":               "Nantes,FR",
    "montpellier":          "Montpellier,FR",
    "toulouse":             "Toulouse,FR",
    "brest":                "Brest,FR",
    "reims":                "Reims,FR",
    "lorient":              "Lorient,FR",
    "metz":                 "Metz,FR",
    "le havre":             "Le Havre,FR",
    "angers":               "Angers,FR",
    "clermont":             "Clermont-Ferrand,FR",
    "auxerre":              "Auxerre,FR",
    "saint-etienne":        "Saint-Etienne,FR",

    # ── Eredivisie ────────────────────────────────────────────────────────
    "ajax":                 "Amsterdam,NL",
    "psv":                  "Eindhoven,NL",
    "psv eindhoven":        "Eindhoven,NL",
    "feyenoord":            "Rotterdam,NL",
    "az alkmaar":           "Alkmaar,NL",
    "az":                   "Alkmaar,NL",
    "twente":               "Enschede,NL",
    "fc twente":            "Enschede,NL",
    "utrecht":              "Utrecht,NL",
    "fc utrecht":           "Utrecht,NL",

    # ── MLS ───────────────────────────────────────────────────────────────
    "la galaxy":            "Los Angeles,US",
    "lafc":                 "Los Angeles,US",
    "los angeles fc":       "Los Angeles,US",
    "inter miami":          "Miami,US",
    "seattle sounders":     "Seattle,US",
    "portland timbers":     "Portland,US",
    "new york city":        "New York,US",
    "new york red bulls":   "New York,US",
    "toronto fc":           "Toronto,CA",
    "montreal impact":      "Montreal,CA",
    "cf montreal":          "Montreal,CA",
    "atlanta united":       "Atlanta,US",
    "orlando city":         "Orlando,US",
    "columbus crew":        "Columbus,US",
    "chicago fire":         "Chicago,US",
    "sporting kc":          "Kansas City,US",
    "fc dallas":            "Dallas,US",
    "houston dynamo":       "Houston,US",
    "real salt lake":       "Salt Lake City,US",
    "colorado rapids":      "Denver,US",
    "san jose earthquakes": "San Jose,US",
    "vancouver whitecaps":  "Vancouver,CA",

    # ── Liga MX ───────────────────────────────────────────────────────────
    "america":              "Mexico City,MX",
    "club america":         "Mexico City,MX",
    "chivas":               "Guadalajara,MX",
    "guadalajara":          "Guadalajara,MX",
    "cruz azul":            "Mexico City,MX",
    "pumas":                "Mexico City,MX",
    "pumas unam":           "Mexico City,MX",
    "tigres":               "Monterrey,MX",
    "tigres uanl":          "Monterrey,MX",
    "monterrey":            "Monterrey,MX",
    "cf monterrey":         "Monterrey,MX",
    "toluca":               "Toluca,MX",
    "santos laguna":        "Torreon,MX",
    "leon":                 "Leon,MX",
    "atlas":                "Guadalajara,MX",
    "necaxa":               "Aguascalientes,MX",
    "queretaro":            "Queretaro,MX",
    "mazatlan":             "Mazatlan,MX",
    "fc juarez":            "Ciudad Juarez,MX",
}


def get_city_for_team(team: str) -> str | None:
    """Busca la ciudad del estadio para un equipo dado."""
    team_lower = team.lower().strip()
    return STADIUM_CITY.get(team_lower)


def fetch_weather(city: str) -> dict | None:
    """
    Llama a OpenWeatherMap para obtener condiciones actuales en una ciudad.

    Returns:
        {
            "temp_c":     float  — temperatura en Celsius
            "rain_mm_h":  float  — precipitación en mm/h (0 si no llueve)
            "wind_ms":    float  — velocidad del viento en m/s
            "snow":       bool   — True si hay nieve
            "fog":        bool   — True si hay niebla
            "description": str  — descripción del tiempo
        }
        None si no hay API key o falla la llamada.
    """
    if not WEATHER_API_KEY:
        return None

    try:
        r = requests.get(
            WEATHER_BASE_URL,
            params={
                "q":     city,
                "appid": WEATHER_API_KEY,
                "units": "metric",
            },
            timeout=WEATHER_TIMEOUT,
        )
        if r.status_code != 200:
            return None

        data = r.json()

        temp_c    = data.get("main", {}).get("temp", 15.0)
        wind_ms   = data.get("wind", {}).get("speed", 0.0)
        rain_mm_h = data.get("rain", {}).get("1h", 0.0)
        snow      = "snow" in data.get("weather", [{}])[0].get("description", "").lower()
        fog_codes = {741, 751, 761, 762}
        weather_id = data.get("weather", [{}])[0].get("id", 800)
        fog       = weather_id in fog_codes
        description = data.get("weather", [{}])[0].get("description", "clear")

        return {
            "temp_c":      round(temp_c,    1),
            "rain_mm_h":   round(rain_mm_h, 2),
            "wind_ms":     round(wind_ms,   1),
            "snow":        snow,
            "fog":         fog,
            "description": description,
        }

    except Exception:
        return None


def get_weather_multiplier(home_team: str) -> float:
    """
    Retorna el multiplicador de lambda_total basado en el clima del estadio.

    Solo afecta el estadio local (el partido se juega ahí).
    Valores < 1.0 reducen los goles esperados (clima adverso).
    Siempre retorna 1.0 si no hay datos de clima disponibles.

    Args:
        home_team: nombre del equipo local (para encontrar la ciudad)

    Returns:
        float en [0.80, 1.00]
    """
    city = get_city_for_team(home_team)
    if not city:
        return 1.0

    weather = fetch_weather(city)
    if not weather:
        return 1.0

    multiplier = 1.0

    # ── Lluvia ────────────────────────────────────────────────────────────
    if weather["rain_mm_h"] >= 5.0:
        multiplier *= 0.93    # lluvia intensa
    elif weather["rain_mm_h"] >= 1.0:
        multiplier *= 0.97    # lluvia ligera

    # ── Viento ────────────────────────────────────────────────────────────
    if weather["wind_ms"] >= 16.0:
        multiplier *= 0.91    # viento muy fuerte
    elif weather["wind_ms"] >= 10.0:
        multiplier *= 0.95    # viento moderado-fuerte

    # ── Nieve ─────────────────────────────────────────────────────────────
    if weather["snow"]:
        multiplier *= 0.90

    # ── Niebla ────────────────────────────────────────────────────────────
    if weather["fog"]:
        multiplier *= 0.96

    # ── Frío extremo ─────────────────────────────────────────────────────
    if weather["temp_c"] < 2.0:
        multiplier *= 0.97

    # ── Calor extremo ─────────────────────────────────────────────────────
    if weather["temp_c"] > 32.0:
        multiplier *= 0.95

    multiplier = round(max(0.80, min(multiplier, 1.00)), 4)

    # ── Log si hay condición adversa notable ─────────────────────────────
    if multiplier < 0.97:
        print(
            f"  🌧  Weather {home_team} ({city}): {weather['description']}"
            f"  rain={weather['rain_mm_h']}mm/h  wind={weather['wind_ms']}m/s"
            f"  → lambda_mult={multiplier}"
        )

    return multiplier
