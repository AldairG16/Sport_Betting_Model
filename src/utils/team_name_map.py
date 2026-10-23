TEAM_NAME_MAP = {
    "leeds": "leeds united",
    "man city": "manchester city",


    # =========================
    # EXISTENTE
    # =========================
    "atletico madrid": "ath madrid",
    # Nombres de ESPN (lineups, espn_matches; 23-oct-26): la historia de
    # alineaciones de estos equipos estaba partida en dos nombres
    "houston dynamo fc": "houston dynamo",
    "chicago fire fc": "chicago fire",
    "red bull new york": "new york red bulls",   # → "ny red bulls" en el 2.º paso de alias
    "red bull bragantino": "bragantino",
    "atletico de san luis": "atl san luis",
    "belgrano (cordoba)": "belgrano",
    "sarmiento (junin)": "sarmiento junin",
    "talleres (cordoba)": "talleres cordoba",
    "gimnasia (mendoza)": "gimnasia mendoza",
    "union (santa fe)": "union santa fe",
    "instituto (cordoba)": "instituto",
    "central cordoba (santiago del estero)": "central cordoba",
    "estudiantes de la plata": "estudiantes",
    "jef united ichihara chiba": "jef united chiba",
    "machida zelvia": "fc machida zelvia",
    "tokyo verdy 1969": "tokyo verdy",
    "kyoto sanga": "kyoto purple sanga",
    "afc bournemouth": "bournemouth",
    "brighton & hove albion": "brighton",
    "olympiacos": "olympiakos piraeus",
    "union st gilloise": "union saint gilloise",
    "stade rennais": "rennes",
    "1 fc heidenheim 1846": "heidenheim",
    "1 fc nurnberg": "nurnberg",
    "1 fc magdeburg": "magdeburg",
    # Nombres de Understat (team_xg_real, player_club_goals; 23-oct-26)
    "borussia mgladbach": "gladbach",
    "mainz 05": "mainz",
    "rasenballsport leipzig": "leipzig",
    "parma calcio 1913": "parma",
    "racing santander": "real racing club de santander",
    "atl madrid": "ath madrid",   # así entró un partido de agosto-26 (lo fusionó el weekly del 5-oct)
    "real sociedad": "sociedad",

    # Ascendidos 2026/27 y nombres de la API sin historial (8-oct-26): la
    # API los escribe con prefijo o sufijo y el historial de football-data
    # sin él, y sin un partido repetido no hay evidencia para aprenderlo.
    # El modelo los veía sin un solo partido previo.
    "fc schalke 04": "schalke 04",
    "deportivo la coruna": "la coruna",
    "cs maritimo": "maritimo",
    "kv kortrijk": "kortrijk",
    "sk beveren": "beveren",
    "le mans fc": "le mans",
    "kalamata fc": "kalamata",
    "ado den haag": "den haag",
    "sc cambuur": "cambuur",
    "amed sk": "amedspor",
    "corum fk": "corum",
    "sc paderborn": "paderborn",
    # 20-oct-26: The Odds API escribe "Erzurum BB" y football-data
    # "Erzurumspor" (BB Erzurumspor, ascendido). Los nombres no se parecen lo
    # suficiente para unirse solos: la historia quedaba partida (2 partidos
    # con un nombre, el resto con el otro) y un partido repetido.
    "erzurum bb": "erzurumspor",

    # =========================
    # 🔥 NUEVOS (PEGAR AQUÍ)
    # =========================

    "argentinos juniors": "argentinos jrs",
    "as monaco": "monaco",
    "atalanta bc": "atalanta",
    "athletic bilbao": "ath bilbao",
    "atletico san luis": "atl san luis",
    "atletico tucuman": "atl tucuman",
    "bayern munich": "bayern munchen",
    "botafogo": "botafogo rj",
    "chapecoense": "chapecoensesc",
    "deportivo riestra": "dep riestra",
    "espanyol": "espanol",
    "estudiantes de rio cuarto": "estudiantes rio cuarto",
    "fc st pauli": "st pauli",
    "flamengo": "flamengo rj",
    "gimnasia la plata": "gimnasia lp",
    "guadalajara": "guadalajara chivas",
    "mazatlan fc": "mazatlan",
    "sarmiento": "sarmiento junin",

    # =========================
    # MLS (CRÍTICO)
    # =========================

    "inter miami": "inter miami cf",
    "lafc": "los angeles fc",
    "los angeles galaxy": "la galaxy",
    "new york city": "new york city fc",
    "ny red bulls": "new york red bulls",
    "seattle sounders": "seattle sounders fc",
    "portland timbers": "portland timbers",
    "sporting kansas city": "sporting kc",
    "san jose earthquakes": "san jose earthquakes",
    "vancouver whitecaps": "vancouver whitecaps fc",
    "toronto": "toronto fc",
    "atlanta united fc": "atlanta united",
    "austin fc": "austin",
    "charlotte fc": "charlotte",
    "fc dallas": "dallas",
    "houston dynamo": "houston dynamo",
    "dc united": "dc united",
    "columbus crew sc": "columbus crew",
    "colorado rapids": "colorado rapids",
    "real salt lake": "real salt lake",
    "orlando city": "orlando city sc",
    "philadelphia union": "philadelphia union",
    "nashville": "nashville sc",
    "montreal": "cf montreal",
    "minnesota united": "minnesota united fc",

    # =========================
    # FIXES IMPORTANTES
    # =========================

    # Inglaterra
    "wolverhampton wanderers": "wolves",
    "brighton and hove albion": "brighton",
    "nottingham forest": "nottm forest",
    "newcastle united": "newcastle",

    # España
    "rayo vallecano": "vallecano",
    "celta vigo": "celta",

    # Alemania
    "koln": "fc koln",
    "hamburger sv": "hamburg",
    "borussia monchengladbach": "gladbach",
    "vfb stuttgart": "stuttgart",
    "vfl wolfsburg": "wolfsburg",
    "eintracht frankfurt": "ein frankfurt",

    # Francia
    "psg": "paris saint germain",
    "rc lens": "lens",

    # Italia
    "as roma": "roma",
    "hellas verona": "verona",
    # NOTA: NUNCA mapear "inter miami" a "inter milan" — son equipos
    # distintos (MLS vs Serie A). Ver src/utils/team_alias_map.py.
}


def normalize_team_name(team):

    if team in TEAM_NAME_MAP:
        return TEAM_NAME_MAP[team]

    return team

