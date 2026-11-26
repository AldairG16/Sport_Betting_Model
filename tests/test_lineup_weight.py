"""
El peso de cada jugador y la defensa (v2.17.0, 26-nov-26).

El dueño: "el psg y el bayern … perdieron, y eso se debe a que ninguno de sus 3
delanteros jugo … cada jugador tiene diferente peso en el equipo", y después
"quiero el 2 pero imagina que ahora es la defensa la que no esta disponible o
se movio". La marca (lineup_alerts.mark_text) sale para cualquier equipo,
favorito o no: le faltan los que meten un tercio de sus goles, o de su línea de
atrás titular (portero y defensas) faltan o juegan en otro puesto 3 o más. El
🎯 se queda (la casa ya lo cuenta en el momio: 66,292 equipo-partidos de ESPN
2019-26); el pick para seguir no va a que gane ese equipo. Sin DB.
"""

from types import SimpleNamespace

import pandas as pd

from src.features import espn_lineups as EL
from src.models import lineup_alerts as LA
from src.models import program_picks as PP
from tests.fake_db import FakeEngine, FakeResult
from tests.test_program_parlays import _mvs, _up

XI = [f"p{i}" for i in range(11)]
# 4-3-3: p0 portero, p1-p4 defensas, p5-p7 medios, p8-p10 delanteros
POS = {"p0": "G", "p1": "RB", "p2": "CD-R", "p3": "CD-L", "p4": "LB", "p5": "CM-R", "p6": "DM", "p7": "CM-L",
       "p8": "RF", "p9": "F", "p10": "LF"}
NAMES = {f"p{i}": f"Jugador {i}" for i in range(16)}


def _history(n=15, positions=True, goals=None):
    """15 partidos con el mismo XI; goles por partido de cada uno (de base: p9 1 y p10 1 en cada uno, p5 1 en 5)."""
    out = []
    for k in range(n):
        g = dict(goals(k) if goals else {"p9": 1, "p10": 1, **({"p5": 1} if k < 5 else {})})
        out.append({"starters": XI, "goals": g, "names": NAMES, "positions": POS if positions else {}})
    return out


def _prof(**kw):
    return LA.team_profile(_history(**kw))


# ── ESPN: la posición de cada titular ─────────────────────────────────────────

def test_the_summary_keeps_the_position_of_each_starter():
    roster = [{"athlete": {"id": f"h{i}", "displayName": f"J{i}"}, "starter": i < 11,
               "position": {"abbreviation": "G" if i == 0 else ("CD" if i < 5 else ("SUB" if i >= 11 else "M"))},
               "stats": []} for i in range(16)]
    data = {"rosters": [{"homeAway": "home", "roster": roster}, {"homeAway": "away", "roster": roster}]}
    s = EL.parse_summary(data)
    assert s["home"]["positions"]["h0"] == "G" and s["home"]["positions"]["h4"] == "CD"
    assert len(s["home"]["positions"]) == 11 and "h12" not in s["home"]["positions"]     # solo los titulares
    ev = {"event": "9", "completed": False}
    (row, _) = EL.lineup_rows("soccer_epl", ev, s, "a", "b", pd.Timestamp("2026-11-28T15:00Z"))
    assert row["positions"].startswith("{") and '"h0": "G"' in row["positions"]


def test_fill_positions_completes_the_recent_history_without_positions():
    """La historia guardada antes de v2.17.0 no tiene posición: fill_positions pide a ESPN solo esos partidos
    (los últimos de cada equipo) y llena la columna; si ESPN ya no lo da, guarda {} para no volver a pedirlo."""
    roster = [{"athlete": {"id": f"x{i}", "displayName": f"J{i}"}, "starter": i < 11,
               "position": {"abbreviation": "G" if i == 0 else "M"}, "stats": []} for i in range(12)]
    summary = {"rosters": [{"homeAway": "home", "roster": roster}, {"homeAway": "away", "roster": roster}]}

    def respond(sql, params):
        if "positions IS NULL" in sql and sql.startswith("SELECT"):
            return FakeResult(rows=[("soccer_epl", "101"), ("soccer_epl", "102"), ("soccer_xx", "103")])
        return FakeResult()

    class R:
        status_code = 200

        def __init__(self, eid):
            self.eid = eid

        def json(self):
            return summary if self.eid == "101" else {}
    get = lambda url, headers=None, timeout=None: R(url.rsplit("=", 1)[-1])
    eng = FakeEngine(respond)
    n = EL.fill_positions(eng, leagues={"soccer_epl": "eng.1"}, get=get, workers=1, verbose=False)
    ups = eng.statements("UPDATE lineups SET positions")
    assert n == 2 and len(ups) == 4                       # 101 con posiciones (2 equipos); 102 sin datos: {}
    assert {p["e"] for _, p in ups} == {"101", "102"}     # 103 es de una liga que ESPN no tiene
    assert all(p["p"] == "{}" for _, p in ups if p["e"] == "102")
    assert any("ADD COLUMN IF NOT EXISTS positions" in s for s, _ in eng.executed)


# ── El perfil y las marcas ────────────────────────────────────────────────────

def test_the_profile_knows_the_usual_position_and_the_goals_of_each_regular():
    prof = _prof()
    assert prof["usual_pos"]["p0"] == "G" and prof["usual_pos"]["p2"] == "CD-R" and prof["pos_n"] == 15
    assert prof["team_goals"] == 35 and prof["goals"]["p9"] == 15
    assert _prof(positions=False)["pos_n"] == 0


def test_attack_mark_when_the_missing_scored_a_third_of_the_goals():
    prof = _prof()                                       # p9 15, p10 15, p5 5 de 35 goles
    a = LA.absences(XI[:9] + ["x1", "x2"], prof)         # sin p9 ni p10: 30 de 35
    assert a["heavy"] == ["Jugador 10", "Jugador 9"] and a["share"] == round(30 / 35, 3)
    assert LA.weight_out(a) and LA.mark_text(a) == ("le faltan Jugador 10 y Jugador 9, que metieron 86 de cada 100 "
                                                    "goles del equipo")
    one = LA.absences([p for p in XI if p != "p5"] + ["x1"], prof)      # sin p5: 5 de 35, poco
    assert not LA.weight_out(one) and LA.mark_text(one) is None
    alone = LA.absences([p for p in XI if p != "p9"] + ["x1"], prof)    # sin p9: 15 de 35 = 43
    assert LA.mark_text(alone) == "le falta Jugador 9, que metió 43 de cada 100 goles del equipo"


def test_attack_mark_needs_enough_goals_to_say_who_scores():
    prof = _prof(goals=lambda k: {"p9": 1} if k < 4 else {})          # 4 goles en 15: no dice nada
    a = LA.absences([p for p in XI if p != "p9"] + ["x1"], prof)
    assert a["share"] == 0.0 and LA.mark_text(a) is None


def test_defense_mark_when_three_of_the_back_line_are_out_or_moved():
    prof = _prof(goals=lambda k: {"p9": 1})
    two = LA.absences(["x1", "x2"] + XI[2:], prof, POS)               # sin portero ni p1: 2, rotación normal
    assert two["back"]["out"] == ["Jugador 0", "Jugador 1"] and not LA.back_changed(two)
    assert LA.mark_text(two) is None
    pos = {**POS, "p3": "CM-L"}                                       # y p3 (central) juega de medio
    three = LA.absences(["x1", "x2"] + XI[2:], prof, pos)
    assert three["back"]["moved"] == [("Jugador 3", "CM-L")] and LA.back_changed(three)
    assert LA.mark_text(three) == "le cambia la defensa: no juegan Jugador 0 y Jugador 1, y Jugador 3 juega de medio"
    gone = LA.absences(["x1", "x2", "x3"] + XI[3:], prof, POS)
    assert LA.mark_text(gone) == "le cambia la defensa: no juegan Jugador 0, Jugador 1 y Jugador 2"
    moved = LA.absences(XI, prof, {**POS, "p1": "RM", "p2": "DM", "p4": "LF"})
    assert LA.mark_text(moved) == ("le cambia la defensa: Jugador 1 juega de medio, Jugador 2 juega de medio, "
                                   "Jugador 4 juega de delantero")


def test_without_positions_in_the_history_the_defense_is_not_judged():
    prof = _prof(positions=False)
    a = LA.absences(["x1", "x2", "x3"] + XI[3:], prof, POS)
    assert a["back"] is None and not LA.back_changed(a)
    # sin la posición de hoy, solo cuenta quién no juega (no quién se movió)
    a = LA.absences(["x1", "x2"] + XI[2:], _prof(), None)
    assert a["back"]["moved"] == [] and len(a["back"]["out"]) == 2


def test_both_marks_and_the_old_shape_still_work():
    a = LA.absences(["x1", "x2", "x3"] + XI[3:9] + ["x4", "x5"], _prof(), POS)
    assert LA.mark_text(a).startswith("le faltan Jugador 10 y Jugador 9, que metieron") and "; le cambia la defensa" in LA.mark_text(a)
    # las bajas escritas a mano (sin heavy ni back), como en los tests de antes: sin marca
    assert LA.mark_text({"missing": ["A"], "top_out": None, "known": True}) is None
    assert LA.mark_text({"missing": [], "known": False}) is None
    assert LA.triggered(a) and len(a["missing"]) == 5                  # la regla de 4+ sigue igual


# ── El pick: marca sin quitar el 🎯 ───────────────────────────────────────────

def _heavy():
    a = LA.absences([p for p in XI if p not in ("p9", "p10")] + ["x1", "x2"], _prof())
    return a, {"missing": [], "top_out": None, "known": True}


def test_the_mark_goes_to_any_team_and_is_saved_with_its_name():
    a, full = _heavy()
    soft = PP.lineup_marks("napoli", "lecce", {"home": full, "away": a})
    assert soft == {"away_win": "A Lecce le faltan Jugador 10 y Jugador 9, que metieron 86 de cada 100 goles del equipo"}
    assert PP.lineup_marks("napoli", "lecce", {"home": a}) == {}          # falta la del visitante
    weak, note, marks = PP.lineup_view("napoli", "lecce", {"home": a, "away": full})
    assert weak == {} and note == "confirmada" and list(marks) == ["home_win"]     # 2 fuera: no es la regla de 4+
    assert PP.mark_of({"away_win": "B", "home_win": "A"}) == "A · B" and PP.mark_of({}) is None
    assert PP.lineup_parts(({}, "confirmada")) == ({}, "confirmada", {})           # lineups_for de antes: dos partes
    assert PP.lineup_parts(None) == ({}, None, {})


def test_a_marked_strong_favourite_keeps_its_target():
    """El dueño eligió (2): el 🎯 se queda; la marca se ve."""
    soft = {"home_win": "A Napoli le faltan Kane y Olise, que metieron 39 de cada 100 goles del equipo"}
    pick, why = PP.decide(_mvs(), SimpleNamespace(**_up()), soft=soft)
    assert why is None and pick["strong"] and pick["market"] == "home_win"
    # la regla de 4+ sí lo quita, como antes
    weak = {"home_win": PP.WEAKENED.format(team="Napoli", n=5, names="a, b, c, d, e")}
    pick, why = PP.decide(_mvs(), SimpleNamespace(**_up()), weak=weak, soft=soft)
    assert why is not None and not (pick or {}).get("strong")


def test_the_pick_to_follow_avoids_the_marked_winner():
    from tests.test_program_picks import _home_fav
    pk, why = PP.choose(_home_fav())
    assert pk["market"] == "home_win"                                # sin marca: el local
    soft = {"home_win": "A LAFC le faltan Jugador 9, que metió 43 de cada 100 goles del equipo"}
    pk, why = PP.choose(_home_fav(), weak=soft)
    assert pk["market"] == "over25" and why is None                  # pasa a lo mejor que queda
    only = [c for c in _home_fav() if c["family"] == "Ganador"]
    pk, why = PP.choose(only, weak=soft)
    assert pk["market"] == "home_win" and why == soft["home_win"]    # si no queda otra cosa, se ve con la marca


def test_build_picks_saves_the_mark_and_never_hides_the_pick():
    from tests.test_program_picks import _mvs as mvs_row, UP, PIN_EVEN
    a, full = _heavy()
    mv = pd.DataFrame([mvs_row(model=(0.51, 0.23, 0.26), pin=(0.43, 0.27, 0.30), over=0.60)])
    up = pd.DataFrame([{**UP, **PIN_EVEN, "consensus_home_odds": 2.20}])
    (row,) = PP.build_picks(mv, up, lineups_for=lambda h, a_, k: PP.lineup_view(h, a_, {"home": a, "away": full}))
    assert row["market"] == "over25" and row["lineup"] == "confirmada"
    assert row["lineup_mark"].startswith("A Atlas le faltan Jugador 10 y Jugador 9")
    (row,) = PP.build_picks(mv, up)
    assert row["market"] == "home_win" and row["lineup_mark"] is None
    assert "lineup_mark" in PP._COLS and any("lineup_mark" in x for x in PP.PICKS_ALTERS)


def test_the_parlay_leg_is_not_touched_by_the_mark():
    from src.models import program_parlays as PL
    from tests.test_program_parlays import _four
    mvs, up = _four()
    soft = {"home_win": "A Napoli le faltan Kane y Olise, que metieron 39 de cada 100 goles del equipo"}
    built = PL.build_parlays(mvs, up, lineups_for=lambda h, a, k: ({}, "confirmada", soft))
    plain = PL.build_parlays(mvs, up)
    legs = lambda ps: [[x["match"] for x in p["legs"]] for p in ps]
    assert legs(built) == legs(plain) and len(legs(plain)[0]) == 4          # las mismas cuatro piernas
    weak = {"home_win": PP.WEAKENED.format(team="Napoli", n=5, names="a, b, c, d, e")}
    four = PL.build_parlays(mvs, up, lineups_for=lambda h, a, k: (weak if h == "napoli" else {}, "confirmada", {}))
    assert all("napoli vs lecce" not in ms for ms in legs(four))            # la regla de 4+ sí la saca


# ── Dónde se ve: Telegram, dashboard, chequeo diario y reporte semanal ────────

def test_telegram_shows_the_mark_one_line_per_team():
    from zoneinfo import ZoneInfo
    from src.models import match_blocks as MB
    from tests.test_match_blocks import _row
    mark = ("A Bayern Munich le faltan Kane y Olise, que metieron 39 de cada 100 goles del equipo · "
            "A 1. FC Köln le cambia la defensa: no juegan A, B y C")
    star = {**_row("bayern munich vs fc koln", "2026-11-21 14:30", lineup="confirmada"), "lineup_mark": mark}
    lines = MB.match_lines(star, ZoneInfo("America/Mexico_City"))
    assert lines[-2] == ("   ⚠️ A Bayern Munich le faltan Kane y Olise, que metieron 39 de cada 100 goles del equipo "
                         "<i>(es una marca: el 🎯 se queda, la casa ya lo cuenta en el momio)</i>")
    assert lines[-1].startswith("   ⚠️ A 1. FC Köln le cambia la defensa")
    follow = {**star, "reason": "No es el muy favorito"}
    assert MB.mark_lines(follow)[0].endswith("del equipo")                 # sin 🎯: sin la nota del 🎯
    assert MB.mark_lines({**star, "lineup_mark": None}) == [] and "lineup_mark" in MB._SQL


def test_the_dashboard_card_and_the_lineups_tab_show_the_mark():
    from pathlib import Path
    from dashboard import lineup_verdict as LV
    from dashboard.visibility import saved_pick
    p = saved_pick({"market": "home_win", "label": "Gana Bayern", "lineup": "confirmada",
                    "lineup_mark": "A Bayern le faltan Kane y Olise, que metieron 39 de cada 100 goles del equipo"})
    assert p["lineup_mark"].startswith("A Bayern") and saved_pick({"market": "x", "label": "y"})["lineup_mark"] == ""
    js = Path("dashboard/ui_es5.js").read_text(encoding="utf-8")
    assert "function markTag(pk)" in js and "formTag(pk) + markTag(pk)" in js
    prof = _prof()
    row = {"kickoff": pd.Timestamp("2026-11-28T15:00Z"), "formation": "4-3-3", "names": NAMES, "positions": POS,
           "starters": [p for p in XI if p not in ("p9", "p10")] + ["x1", "x2"]}
    t = LV.team_lineup(row, prof)
    assert t["mark"].startswith("le faltan Jugador 10 y Jugador 9")
    full = LV.team_lineup({**row, "starters": XI}, prof)
    assert full["mark"] == ""
    v = LV.verdict("Bayern", "Köln", t, full, [], None)
    assert v["marks"] == ["A Bayern le faltan Jugador 10 y Jugador 9, que metieron 86 de cada 100 goles del equipo"]
    assert "⚠️ A Bayern le faltan" in v["text"] and "el 🎯 se queda" in v["text"] and v["level"] == "rotation"
    assert LV.verdict("Bayern", "Köln", full, full, [], None)["marks"] == []


def test_the_daily_check_wants_marks_and_positions():
    from src.utils import output_checks as OC
    cov = OC.positions_coverage([{"sport_key": "soccer_epl", "positions": {"a": "G"}},
                                 {"sport_key": "soccer_epl", "positions": "{}"},
                                 {"sport_key": "soccer_korea_kleague1", "positions": None}])
    assert cov == {"total": 3, "missing": 2, "leagues": ["soccer_korea_kleague1", "soccer_epl"]}
    kick = pd.Timestamp("2026-11-28 15:00", tz="UTC")
    lu = [{"team": "a", "opponent": "b", "is_home": True, "kickoff": kick, "starters": XI},
          {"team": "b", "opponent": "a", "is_home": False, "kickoff": kick, "starters": XI}]
    picks = [{"match": "a vs b", "match_date": kick, "market": "draw", "reason": "x", "lineup": "confirmada",
              "lineup_mark": "A A le faltan…"},
             {"match": "a vs b", "match_date": kick, "market": "draw", "reason": "x", "lineup": "confirmada",
              "lineup_mark": None},
             {"match": "a vs b", "match_date": pd.Timestamp("2026-11-20 15:00", tz="UTC"), "market": "draw",
              "reason": "x", "lineup": "confirmada", "lineup_mark": None}]          # antes de la marca: no cuenta
    lu += [{**r, "kickoff": pd.Timestamp("2026-11-20 15:00", tz="UTC")} for r in lu]
    out = OC.lineup_pick_problems(picks, lu, {})
    assert (out["marked"], out["mark_seen"], out["with_lineup"]) == (1, 2, 3)


def test_the_weekly_block_says_what_the_four_out_rule_took_and_what_the_mark_changed():
    from src.models import lineup_changes as LC
    from tests.test_kickoff_freeze_prelineup import _chg
    took = {**_chg("Home", "Draw", True, False, "win", "loss"), "reason": "Alineación: a X le faltan 5 titulares"}
    took2 = {**_chg("Home", "Draw", True, False, "loss", "win"), "reason": "Alineación: a Y le faltan 4 titulares"}
    marked = {**_chg("Home", "Over", False, False, "loss", "win"), "lineup_mark": "A X le faltan…"}
    plain = _chg("Btts", "Under", True, True, "loss", "win")
    s = LC.summarize([took, took2, marked, plain])
    assert (s["removed_star"], s["removed_won"]) == (2, 1) and round(s["removed_units"], 2) == -0.1
    assert (s["marked_changed"], s["marked_before"], s["marked_after"]) == (1, 0, 1)
    text = "\n".join(LC.rule_lines(s))
    assert "La regla de 4 o más titulares fuera quitó 2 🎯: el 🎯 de antes habría ganado 1 de 2 (-5)." in text
    assert "Con la marca de bajas que pesan cambió 1 pick para seguir: el de antes ganó 0 y el nuevo 1." in text
    assert LC.rule_lines(LC.summarize([plain])) == []
