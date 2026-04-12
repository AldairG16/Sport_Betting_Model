TEAM_NAME_MAP = {

    # =========================
    # EXISTENTE
    # =========================
    "atletico madrid": "ath madrid",
    "real sociedad": "sociedad",

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

    "wolverhampton wanderers": "wolves",
    "koln": "fc koln",
    "hamburger sv": "hamburg",
    "borussia monchengladbach": "gladbach",
    # Inglaterra
    "wolverhampton wanderers": "wolves",
    "brighton and hove albion": "brighton",
    "nottingham forest": "nottm forest",
    "newcastle united": "newcastle",

    # España
    "rayo vallecano": "vallecano",
    "celta vigo": "celta",

    # Alemania
    "vfb stuttgart": "stuttgart",
    "vfl wolfsburg": "wolfsburg",
    "eintracht frankfurt": "ein frankfurt",

    # Francia
    "psg": "paris saint germain",
    "rc lens": "lens",

    # Italia
    "as roma": "roma",
    "hellas verona": "verona",
    "inter miami": "inter milan",
}


def normalize_team_name(team):

    if team in TEAM_NAME_MAP:
        return TEAM_NAME_MAP[team]

    return team

