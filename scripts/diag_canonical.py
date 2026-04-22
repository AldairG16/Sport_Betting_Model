import sys, io; sys.path.insert(0, '.')
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from config.database import engine
import pandas as pd
from sqlalchemy import text

with engine.connect() as c:
    print('=== MLS teams en matches ===')
    r = pd.read_sql(text("""
        SELECT DISTINCT home_team as team FROM matches WHERE league='soccer_usa_mls'
        UNION
        SELECT DISTINCT away_team FROM matches WHERE league='soccer_usa_mls'
        ORDER BY team
    """), c)
    print(r.to_string())

    print()
    print('=== Equipos duplicados/ambiguos sospechosos ===')
    suspects = ['chivas','guadalajara','estudiantes','independiente',
                'los angeles','la galaxy','lafc','inter',
                'seattle','portland','san jose','toronto','new york',
                'montreal','vancouver','minnesota','orlando','nashville',
                'columbus','colorado','real salt','philadelphia','houston',
                'austin','dallas','charlotte','cincinnati','atlanta',
                'dc united','rivadavia']
    for s in suspects:
        r2 = pd.read_sql(text(f"""
            SELECT DISTINCT home_team FROM matches WHERE home_team ILIKE '%{s}%'
            UNION SELECT DISTINCT away_team FROM matches WHERE away_team ILIKE '%{s}%'
            ORDER BY 1
        """), c)
        if len(r2) > 1:
            print(f"  '{s}':")
            for v in r2.iloc[:,0].tolist():
                print(f"    {v!r}")

    # Idempotencia test
    print()
    print('=== Test idempotencia normalize_team ===')
    from src.utils.team_normalizer import normalize_team
    test_cases = [
        'inter miami', 'inter miami cf', 'inter milan',
        'lafc', 'los angeles fc', 'la galaxy', 'los angeles galaxy',
        'toronto', 'toronto fc', 'seattle sounders', 'seattle sounders fc',
        'chivas', 'guadalajara', 'guadalajara chivas',
        'estudiantes', 'estudiantes lp', 'estudiantes la plata',
        'independiente', 'independiente rivadavia', 'ind rivadavia',
        'atletico madrid', 'real sociedad',
    ]
    for name in test_cases:
        once = normalize_team(name)
        twice = normalize_team(once)
        marker = '' if once == twice else '  ❌ NO IDEMPOTENT'
        print(f"  {name!r:35s} -> {once!r:35s} -> {twice!r}{marker}")
