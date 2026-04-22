"""
DB Cleanup: consolida nombres de equipos a forma canónica.

Ejecuta TODO dentro de una transacción. Si algo falla, rollback.
Al final muestra conteos pre/post para validar.

Canonical forms:
- Inter Miami MLS (DB había: 'inter milan' en soccer_usa_mls)  -> 'inter miami'
- Chivas                 (DB había: 'guadalajara chivas')      -> 'chivas'
- Estudiantes (La Plata) (DB había: 'estudiantes lp', 'estudiantes la plata') -> 'estudiantes'
- Independiente Rivadavia (DB había: 'Independiente Rivadavia') -> 'ind rivadavia'
"""
import sys, io
sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

from config.database import engine
from sqlalchemy import text

UPDATES = [
    # matches: inter milan -> inter miami (solo MLS)
    ("matches.home_team inter milan (MLS)",
     "UPDATE matches SET home_team='inter miami' WHERE home_team='inter milan' AND league='soccer_usa_mls'"),
    ("matches.away_team inter milan (MLS)",
     "UPDATE matches SET away_team='inter miami' WHERE away_team='inter milan' AND league='soccer_usa_mls'"),
    ("matches.team_home_norm inter milan (MLS)",
     "UPDATE matches SET team_home_norm='inter miami' WHERE team_home_norm='inter milan' AND league='soccer_usa_mls'"),
    ("matches.team_away_norm inter milan (MLS)",
     "UPDATE matches SET team_away_norm='inter miami' WHERE team_away_norm='inter milan' AND league='soccer_usa_mls'"),

    # matches: guadalajara chivas -> chivas
    ("matches.home_team guadalajara chivas",
     "UPDATE matches SET home_team='chivas' WHERE home_team='guadalajara chivas'"),
    ("matches.away_team guadalajara chivas",
     "UPDATE matches SET away_team='chivas' WHERE away_team='guadalajara chivas'"),
    ("matches.team_home_norm guadalajara chivas",
     "UPDATE matches SET team_home_norm='chivas' WHERE team_home_norm='guadalajara chivas'"),
    ("matches.team_away_norm guadalajara chivas",
     "UPDATE matches SET team_away_norm='chivas' WHERE team_away_norm='guadalajara chivas'"),

    # matches: estudiantes lp / la plata -> estudiantes
    ("matches.home_team estudiantes variants",
     "UPDATE matches SET home_team='estudiantes' WHERE home_team IN ('estudiantes lp','estudiantes la plata')"),
    ("matches.away_team estudiantes variants",
     "UPDATE matches SET away_team='estudiantes' WHERE away_team IN ('estudiantes lp','estudiantes la plata')"),
    ("matches.team_home_norm estudiantes variants",
     "UPDATE matches SET team_home_norm='estudiantes' WHERE team_home_norm IN ('estudiantes lp','estudiantes la plata')"),
    ("matches.team_away_norm estudiantes variants",
     "UPDATE matches SET team_away_norm='estudiantes' WHERE team_away_norm IN ('estudiantes lp','estudiantes la plata')"),

    # matches: Independiente Rivadavia (uppercase) -> ind rivadavia
    ("matches.home_team Independiente Rivadavia (upper)",
     "UPDATE matches SET home_team='ind rivadavia' WHERE home_team='Independiente Rivadavia'"),
    ("matches.away_team Independiente Rivadavia (upper)",
     "UPDATE matches SET away_team='ind rivadavia' WHERE away_team='Independiente Rivadavia'"),

    # matches: CA Tigre BA (uppercase) -> tigre  (detectado en el mismo row)
    ("matches.home_team CA Tigre BA (upper)",
     "UPDATE matches SET home_team='tigre' WHERE home_team='CA Tigre BA'"),
    ("matches.away_team CA Tigre BA (upper)",
     "UPDATE matches SET away_team='tigre' WHERE away_team='CA Tigre BA'"),

    # bets_history: inter milan -> inter miami (solo MLS)
    ("bets_history.match inter milan (MLS)",
     "UPDATE bets_history SET match=REPLACE(match,'inter milan','inter miami') WHERE match ILIKE '%inter milan%' AND league='soccer_usa_mls'"),

    # upcoming_matches: Independiente Rivadavia (uppercase) -> ind rivadavia
    ("upcoming_matches.home_team Ind Rivadavia (upper)",
     "UPDATE upcoming_matches SET home_team='ind rivadavia' WHERE home_team='Independiente Rivadavia'"),
    ("upcoming_matches.away_team Ind Rivadavia (upper)",
     "UPDATE upcoming_matches SET away_team='ind rivadavia' WHERE away_team='Independiente Rivadavia'"),
]

with engine.begin() as c:
    print("=" * 70)
    print("EJECUTANDO CLEANUP (transacción única)")
    print("=" * 70)
    total = 0
    for label, sql in UPDATES:
        try:
            result = c.execute(text(sql))
            n = result.rowcount
            total += n
            marker = "✓" if n >= 0 else "×"
            print(f"  {marker} {label:55s} -> {n} rows")
        except Exception as e:
            print(f"  × {label:55s} ERROR: {e}")
            raise

    print()
    print(f"TOTAL ROWS AFECTADAS: {total}")
    print()
    print("=" * 70)
    print("VERIFICACIÓN POST-CLEANUP")
    print("=" * 70)

    checks = [
        ("matches inter milan MLS residual",
         "SELECT COUNT(*) FROM matches WHERE league='soccer_usa_mls' AND (home_team='inter milan' OR away_team='inter milan')"),
        ("matches guadalajara chivas residual",
         "SELECT COUNT(*) FROM matches WHERE home_team='guadalajara chivas' OR away_team='guadalajara chivas'"),
        ("matches estudiantes lp/la plata residual",
         "SELECT COUNT(*) FROM matches WHERE home_team IN ('estudiantes lp','estudiantes la plata') OR away_team IN ('estudiantes lp','estudiantes la plata')"),
        ("matches Independiente Rivadavia upper residual",
         "SELECT COUNT(*) FROM matches WHERE home_team='Independiente Rivadavia' OR away_team='Independiente Rivadavia'"),
        ("bets_history inter milan MLS residual",
         "SELECT COUNT(*) FROM bets_history WHERE match ILIKE '%inter milan%' AND league='soccer_usa_mls'"),
        ("upcoming_matches Ind Rivadavia upper residual",
         "SELECT COUNT(*) FROM upcoming_matches WHERE home_team='Independiente Rivadavia' OR away_team='Independiente Rivadavia'"),
    ]
    all_ok = True
    for label, sql in checks:
        n = c.execute(text(sql)).scalar()
        ok = (n == 0)
        if not ok:
            all_ok = False
        print(f"  {'✓' if ok else '×'} {label:55s} -> {n}")

    if not all_ok:
        print("\n⚠  Alguna residual no es 0 — revisa manualmente.")
    else:
        print("\n✅ Todo limpio.")
