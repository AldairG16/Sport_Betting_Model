"""
Picks que el closing rehízo con el partido ya empezado (17-nov-26).

El 8-nov The Odds API tenía Iraklis–Panathinaikos a las 18:00 y empezó a las 15:30 (ESPN). La hora de ESPN
solo se emparejaba a ±90 min (live_scores.AHEAD_H y KICKS_WINDOW_S lo arreglan): el pick no se fijó y el
closing lo rehízo con momios EN JUEGO hasta las 17:33. El 🎯 "Gana el visitante" (Panathinaikos a -380, ganó
2-1) se volvió "Menos de 3.5 goles" sin 🎯, así que el 🎯 ganado no contó y sí contó un pick hecho viendo el
partido.

Esto rehace esos picks como estaban al empezar el partido: con el análisis y la última foto de momios de ANTES
de la hora real (la más temprana entre la del sistema y la de ESPN), con la regla de hoy (`decide`), y los
liquida con la misma función que el evening. Solo desde SINCE (la regla del 🎯 de v2.9.0): antes, rehacerlo
cambiaría la regla, no el error.

Ensayo por defecto. Con --apply guarda la fila de antes en program_picks_fix_backup (como JSON) y la reemplaza.

    python scripts/fix_inplay_picks.py            # qué cambiaría
    python scripts/fix_inplay_picks.py --apply
"""
import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from sqlalchemy import text

SINCE = "2026-11-06"
GRACE_MIN = 2                    # el closing tarda; más que esto después del inicio = momios en juego
FINALS_WINDOW_H = 36             # el final de ESPN del mismo partido (output_checks.FINALS_WINDOW_H)
BACKUP_DDL = """
    CREATE TABLE IF NOT EXISTS program_picks_fix_backup (
        id SERIAL PRIMARY KEY, pick_id INT NOT NULL, row JSONB NOT NULL,
        backup_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), backup_action TEXT NOT NULL
    )
"""
REPLACE_COLS = ("market", "label", "family", "reason", "model_prob", "market_prob", "lean", "min_us", "best_odds",
                "playdoit_odds", "value", "star_note")


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def real_kickoffs(picks: list, kicks: list, finals: list, normalize=None) -> dict:
    """Función pura: {id: hora real} de los picks guardados más de GRACE_MIN después de su inicio real (la
    más temprana entre la del sistema, la de espn_kickoffs y la del final de ESPN en live_scores)."""
    from src.models.program_picks import match_key
    idx, fin = {}, {}
    for k in kicks:
        idx.setdefault(match_key(k["home"], k["away"], normalize), []).append((_utc(k["kickoff"]),
                                                                             _utc(k["espn_kickoff"])))
    for f in finals:
        fin.setdefault(match_key(f["home"], f["away"], normalize), []).append(_utc(f["kickoff"]))
    out = {}
    for p in picks:
        h, _, a = str(p["match"]).partition(" vs ")
        key, kick = match_key(h, a, normalize), _utc(p["match_date"])
        espn = [e for s, e in idx.get(key, []) if abs((s - kick).total_seconds()) <= 3 * 3600]
        espn += [e for e in fin.get(key, []) if abs((e - kick).total_seconds()) <= FINALS_WINDOW_H * 3600]
        real = min([kick] + espn)
        if _utc(p["picked_at"]) > real + pd.Timedelta(minutes=GRACE_MIN):
            out[p["id"]] = real
    return out


def rebuild(mvs_row: dict, snap: dict, then: dict | None, bias=None, paused=()) -> dict:
    """Función pura: la fila del pick con el análisis y la foto de momios de antes de empezar, con la regla
    de hoy (program_picks.decide) y su nota de por qué no tiene 🎯."""
    from src.models import program_picks as PP
    u = SimpleNamespace(**snap)
    pk, why = PP.decide(mvs_row, u, then, None, None, bias, {}, paused)
    note = None
    if pk and why:
        home, _, away = str(snap["match"]).partition(" vs ")
        note = PP.no_star_note(PP._candidates_of(mvs_row, u, None, None, bias), mvs_row.get("league"), {},
                               (home, away))
    return PP._row(mvs_row, mvs_row["match_date"], snap["match"], pk, why, star_note=note)


def main(apply: bool = False) -> int:
    from config.database import engine
    from src.models import program_picks as PP
    from src.models.filter_learner import paused_regions
    from src.models.save_bets import _preload_matches, plan_shadow_resolutions
    from src.utils.team_normalizer import normalize_team

    picks = pd.read_sql(text("""
        SELECT * FROM program_picks
        WHERE source IS NULL AND market IS NOT NULL AND result IN ('win', 'loss', 'push')
          AND match_date >= CAST(:s AS timestamptz) AND match_date < NOW() - INTERVAL '3 hours'
    """), engine, params={"s": SINCE})
    kicks = pd.read_sql(text("SELECT home, away, kickoff, espn_kickoff FROM espn_kickoffs "
                             "WHERE kickoff >= CAST(:s AS timestamptz) - INTERVAL '1 day'"), engine, params={"s": SINCE})
    finals = pd.read_sql(text("SELECT home, away, kickoff FROM live_scores WHERE status <> 'postponed' "
                              "AND kickoff >= CAST(:s AS timestamptz) - INTERVAL '2 days'"), engine, params={"s": SINCE})
    late = real_kickoffs(picks.to_dict("records"), kicks.to_dict("records"), finals.to_dict("records"), normalize_team)
    if not late:
        print("✅ Ningún pick rehecho con el partido ya empezado desde", SINCE)
        return 0
    bias, paused = PP.load_bias(engine), paused_regions(engine)
    plan = []
    for p in picks[picks["id"].isin(late)].to_dict("records"):
        real = late[p["id"]]
        mvs = pd.read_sql(text("SELECT * FROM model_vs_sharp WHERE match = :m AND match_date = :d "
                               "AND created_at < :r ORDER BY created_at DESC LIMIT 1"),
                          engine, params={"m": p["match"], "d": p["match_date"], "r": real.to_pydatetime()})
        snaps = pd.read_sql(text(PP._SNAPS_SQL + " AND match = :m"), engine, params={
            "a": (_utc(p["match_date"]) - pd.Timedelta(hours=3)).to_pydatetime(),
            "b": (_utc(p["match_date"]) + pd.Timedelta(hours=3)).to_pydatetime(), "m": p["display"]})
        before = snaps[pd.to_datetime(snaps["captured_at"], utc=True) < real] if not snaps.empty else snaps
        if mvs.empty or before.empty:
            print(f"⚠️  {p['match']}: sin análisis o sin momios de antes de las {real:%H:%M} — se deja como está")
            continue
        r = mvs.iloc[0].to_dict()
        snap = before.sort_values("captured_at").iloc[-1].to_dict()
        idx = PP._snap_index(snaps, normalize_team)
        then = PP._snap_before(idx, PP.match_key(*str(p["match"]).split(" vs "), normalize_team), p["match_date"],
                               r["created_at"])
        new = rebuild(r, snap, then, bias, paused)
        plan.append((p, real, snap["captured_at"], new))

    rows = pd.DataFrame([{"id": p["id"], "match": p["match"], "match_date": p["match_date"], "market": new["market"],
                          "league": p["league"], "playdoit_odds": new["playdoit_odds"]}
                         for p, _, _, new in plan if new["market"]])
    settled = {}
    if not rows.empty:
        priced = rows.assign(odds=rows["playdoit_odds"].fillna(2.0).astype(float))
        out = plan_shadow_resolutions(priced, _preload_matches(rows["match_date"]))
        out = PP._with_early_payout(engine, out, priced)
        settled = {s["id"]: s for s in out}
    for p, real, at, new in plan:
        s = settled.get(p["id"], {})
        star_old, star_new = p["reason"] is None, new["market"] is not None and new["reason"] is None
        print(f"{p['match']} ({_utc(p['match_date']):%d-%m %H:%M} UTC; empezó {real:%H:%M}; guardado "
              f"{_utc(p['picked_at']):%H:%M}): '{p['label']}'{' 🎯' if star_old else ''} {p['result']} "
              f"{p['profit']} → '{new['label']}'{' 🎯' if star_new else ''} {s.get('result')} {s.get('profit')}"
              f" (momios de {_utc(at):%H:%M})")
    if not apply:
        print("Ensayo: nada cambió. Con --apply se guarda la fila de antes en program_picks_fix_backup y se rehace.")
        return len(plan)
    with engine.begin() as conn:
        conn.execute(text(BACKUP_DDL))
        for p, _, _, new in plan:
            s = settled.get(p["id"])
            if new["market"] and s is None:
                print(f"⚠️  {p['match']}: el pick rehecho no se pudo liquidar — se deja como está")
                continue
            conn.execute(text("INSERT INTO program_picks_fix_backup (pick_id, row, backup_action) "
                              "SELECT id, to_jsonb(p), 'inplay_repick' FROM program_picks p WHERE id = :id"),
                         {"id": p["id"]})
            sets = ", ".join(f"{c} = :{c}" for c in REPLACE_COLS)
            conn.execute(text(f"""
                UPDATE program_picks SET {sets}, prev_label = NULL, prev_star = NULL, changed_at = NULL,
                    result = :result, profit = :profit, pa_paid = :pa_paid, resolved_at = NOW()
                WHERE id = :id
            """), {**{c: new[c] for c in REPLACE_COLS}, "id": p["id"],
                   "result": s["result"] if s else None, "profit": s["profit"] if s else None,
                   "pa_paid": s.get("pa_paid") if s else None})
    print(f"✅ {len(plan)} pick(s) rehecho(s) como estaban al empezar el partido (respaldo en program_picks_fix_backup)")
    return len(plan)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true")
    main(ap.parse_args().apply)
