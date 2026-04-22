TEAM_ALIASES = {

    # errores comunes
    "amrica": "america",
    "mazatln": "mazatlan",
    "jurez": "juarez",
    "quertaro": "queretaro",

    # acentos rotos
    "atltico madrid": "atletico madrid",
    "atltico san luis": "atletico san luis",
    "atltico tucuman": "atletico tucuman",
    "atltico huracn": "huracan",

    # brasil
    "grmio": "gremio",
    "bragantinosp": "bragantino",

    # argentina
    "central crdoba": "central cordoba",
    "velez sarsfield ba": "velez sarsfield",
    "dep riestra": "deportivo riestra",
    "ca tigre ba": "tigre",
    "ca osasuna": "osasuna",
    # NOTA: "independiente rivadavia" está más abajo mapeado a "ind rivadavia"
    # (forma canónica), NO se debe mapear a "independiente" porque es otro club.

    # europa
    "kln": "koln",
    "len": "lens",

    # mls
    "los angeles": "lafc",
    # MLS
    "inter miami": "inter miami cf",
    "lafc": "los angeles fc",
    "los angeles galaxy": "la galaxy",
    "new york city": "new york city fc",
    "ny red bulls": "new york red bulls",
    "seattle sounders": "seattle sounders fc",
    "sporting kansas city": "sporting kc",
    "vancouver whitecaps": "vancouver whitecaps fc",
    "toronto": "toronto fc",
    "atlanta united fc": "atlanta united",
    "austin fc": "austin",
    "charlotte fc": "charlotte",
    "fc dallas": "dallas",
    "columbus crew sc": "columbus crew",
    "orlando city": "orlando city sc",
    "nashville": "nashville sc",
    "montreal": "cf montreal",
    "minnesota united": "minnesota united fc",

    # Brasil
    "atletico mineiro": "atletico mineiro mg",
    "atletico paranaense": "athletico pr",
    "vasco da gama": "vasco",
    "flamengo": "flamengo rj",
    "botafogo": "botafogo rj",

    # Argentina
    "racing club": "racing",
    "talleres": "talleres cordoba",
    "independiente rivadavia": "ind rivadavia",
    "estudiantes la plata": "estudiantes",
    "estudiantes lp": "estudiantes",

    # México
    "tigres": "tigres uanl",
    "pumas": "pumas unam",
    "guadalajara": "chivas",
    "guadalajara chivas": "chivas",

    # Europa fixes
    "wolverhampton wanderers": "wolves",
    "koln": "fc koln",
    "hamburger sv": "hamburg",
    "borussia monchengladbach": "gladbach",
    "as monaco": "monaco",
    "atalanta bc": "atalanta",
}

TEAM_ALIASES.update({

    # =========================
    # FIX CURRENT MISSING
    # =========================

    "bayern": "bayern munich",

    # MLS inversions — consolidan a forma canónica CORTA
    # (NAME_MAP expande, aquí contraemos en 3er pass)
    "inter miami cf": "inter miami",
    "los angeles fc": "lafc",
    # NOTA: "la galaxy" NO se mapea — es la forma canónica corta;
    # "los angeles galaxy" → "la galaxy" (main dict).

    "fc cincinnati": "cincinnati",
    "fc juarez": "juarez",

    "toronto fc": "toronto",
    "cf montreal": "montreal",

    "orlando city sc": "orlando city",
    "nashville sc": "nashville",
    "minnesota united fc": "minnesota united",

    "new york city fc": "new york city",
    "new york red bulls": "ny red bulls",

    "seattle sounders fc": "seattle sounders",

    "vancouver whitecaps fc": "vancouver whitecaps",

    "west ham united": "west ham",
    "tottenham hotspur": "tottenham",

    # argentina / brasil
    "belgrano de cordoba": "belgrano",
    "instituto de cordoba": "instituto",
    "sarmiento de junin": "sarmiento",
    "bragantino sp": "bragantino",

    # europa
    "1 fc koln": "koln",
    "1 fc heidenheim": "heidenheim",
    "fsv mainz 05": "mainz",

    "sc freiburg": "freiburg",
    "tsg hoffenheim": "hoffenheim",

    # españa
    "elche cf": "elche",

    # otros
    "rb leipzig": "leipzig",
    # "paris fc" es un equipo distinto a PSG — NO se mapea

})