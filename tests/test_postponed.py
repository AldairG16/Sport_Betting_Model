"""
tests/test_postponed.py
=======================
Partidos pospuestos y reprogramados con la fecha equivocada (auditoría 9-nov-26).

- ESPN da un partido pospuesto por terminado sin jugarse ("Postponed", 0-0) y
  live_scores lo guardaba 'live' 0-0: el dashboard lo mostraba en vivo.
- Ese horario pospuesto valía como la fecha de ESPN de un reprogramado.
  Nacional vs Académico de Viseu (del 7 al 8-nov): se anuló el partido real del
  8 y el del 7 se liquidó con su resultado.
- Anular no tenía vuelta. Kristiansund vs Vålerenga (del 7 al 9-nov): una
  decisión a las 15:02 del 7 anuló las 8 candidatas sombra del 9, y a las
  15:55 se anularon las del 7.
"""

import contextlib

import pandas as pd

from src.features import live_scores as LS
from src.models import rescheduled as R

NAC = "nacional vs academico de viseu"
KRI = "kristiansund bk vs valerenga"
T = lambda s: pd.Timestamp(s, tz="UTC")


def _ev(eid, home, away, hg, ag, state="post", completed=True, detail="FT"):
    comp = {"status": {"type": {"state": state, "completed": completed, "shortDetail": detail}},
            "competitors": [{"homeAway": "home", "score": str(hg), "team": {"id": "1", "displayName": home}},
                            {"homeAway": "away", "score": str(ag), "team": {"id": "2", "displayName": away}}]}
    return {"id": eid, "date": "2026-11-07T15:30Z", "competitions": [comp]}


# ─── ESPN: pospuesto, sin marcador ───────────────────────────────────────────

def test_a_postponed_match_is_saved_as_postponed_without_a_score():
    rows = LS.parse_live({"events": [
        _ev("401885398", "Nacional", "Académico de Viseu", 0, 0, completed=False, detail="Postponed"),
        _ev("2", "Benfica", "Porto", 2, 1),
        _ev("3", "Braga", "Arouca", 1, 0, state="in", completed=False, detail="67'")]})
    off, fin, live = rows
    assert off["status"] == LS.POSTPONED == "postponed" and off["clock"] == "se pospuso"
    assert (off["home"], off["away"]) == ("Nacional", "Académico de Viseu")
    assert all(off[c] is None for c in ("home_goals", "away_goals", "home_corners", "home_cards", "home_max_lead"))
    assert (fin["status"], fin["home_goals"]) == ("final", 2) and (live["status"], live["clock"]) == ("live", "67'")
    # el final de la noche (backfill_finals) no lo toma
    assert LS.final_rows("x", [off], [], set(), pd.Timestamp("2026-11-01").date()) == []


# ─── la fecha que vale ───────────────────────────────────────────────────────

def _rows(match, *dates_written):
    return [{"match": match, "match_date": T(d), "written": T(w)} for d, w in dates_written]


def test_with_the_postponed_time_left_out_the_real_date_wins():
    rows = _rows(NAC, ("2026-11-07 15:30", "2026-11-07 15:02"), ("2026-11-08 15:30", "2026-11-08 03:19"))
    espn = {NAC: [T("2026-11-08 15:30")]}                   # el del 7 (pospuesto) ya no viene
    assert R.phantom_dates(rows, espn) == {(NAC, T("2026-11-07 15:30"))}
    assert R.current_dates(rows, espn) == {NAC: T("2026-11-08 15:30")}
    # con el pospuesto adentro ganaba el horario viejo: el defecto
    assert R.current_dates(rows, {NAC: [T("2026-11-07 15:30"), T("2026-11-08 15:30")]}) == {NAC: T("2026-11-07 15:30")}


class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=None):
        self.log.append((" ".join(str(sql).split()), params))
        return type("Res", (), {"rowcount": 1})()


class _Engine:
    def __init__(self):
        self.log = []

    @contextlib.contextmanager
    def begin(self):
        yield _Conn(self.log)


def test_what_an_earlier_decision_voided_comes_back_on_the_real_date(monkeypatch, capsys):
    """Kristiansund: el feed ya solo trae el 9; lo anulado del 9 se recupera
    (solo lo anulado ANTES de su partido) y se anula el 7."""
    rows = _rows(KRI, ("2026-11-07 15:00", "2026-11-07 12:09"), ("2026-11-09 18:00", "2026-11-09 03:10"))
    seen = []

    def fake_read_sql(sql, engine, params=None):
        s = str(sql)
        seen.append(s)
        if "FROM live_scores" in s:
            return pd.DataFrame(columns=["home", "away", "kickoff"])
        if "FROM upcoming_matches" in s:
            return pd.DataFrame([{"match": KRI, "match_date": T("2026-11-09 18:00")}])
        if "FROM shadow_bets " in s or "FROM model_vs_sharp " in s:
            return pd.DataFrame(rows)
        raise RuntimeError('relation "x" does not exist')
    monkeypatch.setattr(R.pd, "read_sql", fake_read_sql)
    eng = _Engine()
    out = R.void_rescheduled(engine=eng)
    backs = [(s, p) for s, p in eng.log if "SET result = NULL" in s]
    assert [(s.split()[1], p["match_date"]) for s, p in backs] == [("shadow_bets", T("2026-11-09 18:00"))]
    assert "result = 'stale' AND resolved_at < match_date" in backs[0][0]
    voids = [(s.split()[1], p["match_date"]) for s, p in eng.log if "stale', profit" in s or "skipped_at = NOW()" in s]
    assert sorted(voids) == [("model_vs_sharp", T("2026-11-07 15:00")), ("shadow_bets", T("2026-11-07 15:00"))]
    # primero se recupera y luego se anula, en la misma transacción
    assert eng.log.index(backs[0]) < min(i for i, (s, _) in enumerate(eng.log) if "stale', profit" in s)
    assert out["restored"] == {"shadow_bets": 1, "model_vs_sharp": 1}      # el análisis, desde el 11-nov
    assert "recuperadas en la fecha que vale" in capsys.readouterr().out
    espn_sql = next(s for s in seen if "FROM live_scores" in s)
    assert "status IS DISTINCT FROM 'postponed'" in espn_sql and "NOT ILIKE 'postponed" in espn_sql


def test_only_the_rule_that_voids_before_kickoff_is_undone():
    """Las otras anulaciones ('sin datos') son días después del partido. En
    model_vs_sharp, skipped_at también lo pone side_bias.mark_skipped: solo se
    recupera lo marcado por esta regla (skip_reason, 11-nov-26)."""
    assert set(R.RESTORE_SQL) == {"program_picks", "program_parlays", "shadow_bets", "model_proposals",
                                  "model_vs_sharp"}
    for t, sql in R.RESTORE_SQL.items():
        if t == "model_vs_sharp":
            assert "skip_reason = 'reprogramado' AND skipped_at < match_date" in sql
        else:
            assert "result = 'stale' AND resolved_at < match_date" in sql


# ─── chequeo de salidas §20 ──────────────────────────────────────────────────

def test_the_check_flags_a_voided_real_date_and_a_settled_postponed_time():
    current = {NAC: T("2026-11-08 15:30"), KRI: T("2026-11-09 18:00")}
    voided = [{"table": "program_picks", "match": NAC, "match_date": T("2026-11-08 15:30")},
              {"table": "shadow_bets", "match": KRI, "match_date": T("2026-11-07 15:00")}]   # la vieja: bien
    settled = [{"match": NAC, "match_date": T("2026-11-07 15:30")}, {"match": "x vs y", "match_date": T("2026-11-07 15:30")}]
    postponed = {(NAC, T("2026-11-07 15:30"))}
    a, b = R.date_problems(current, voided, settled, postponed)
    assert "1 con filas anuladas en la fecha que vale" in a and NAC in a and "[program_picks]" in a
    assert "1 picks cobrados de un horario que ESPN dio por pospuesto" in b and NAC in b
    assert R.date_problems(current, voided[1:], settled[1:], postponed) == []


def test_the_check_runs_every_evening(monkeypatch):
    import src.utils.output_checks as OC
    src = open(OC.__file__, encoding="utf-8").read()
    assert "RS.check_dates(engine" in src and "reprogramados en su fecha" in src


# ─── lo que se pide y lo que falta ───────────────────────────────────────────

def test_an_old_time_or_a_postponed_match_does_not_ask_for_its_league(monkeypatch):
    """Kristiansund (horario viejo) y Estoril vs Marítimo (pospuesto) hacían
    pedir Noruega y Portugal en cada corrida."""
    import scripts.fetch_results as FR
    from src.utils.output_checks import PLAYED_ANALYSIS_SQL
    assert "skipped_at IS NULL" in PLAYED_ANALYSIS_SQL and "s.status = 'postponed'" in PLAYED_ANALYSIS_SQL
    seen = []
    monkeypatch.setattr(FR.pd if hasattr(FR, "pd") else __import__("pandas"), "read_sql",
                        lambda sql, *a, **k: seen.append(str(sql)) or pd.DataFrame(
                            columns=["match", "match_date", "league", "h", "a", "d", "home_goals", "away_goals"]))
    FR.leagues_needing_results(2)
    assert PLAYED_ANALYSIS_SQL in seen[0]
    import src.utils.output_checks as OC
    assert "q(head + PLAYED_ANALYSIS_SQL, d=days)" in open(OC.__file__, encoding="utf-8").read()


def test_without_live_scores_the_missing_results_check_still_runs(monkeypatch):
    """Instalación nueva: live_scores nace con el closing."""
    import src.utils.output_checks as OC
    seen = []

    def fake(sql, con=None, params=None, **k):
        s = " ".join(str(sql).split())
        seen.append(s)
        if "FROM live_scores" in s:
            raise RuntimeError('relation "live_scores" does not exist')
        return pd.DataFrame(columns=["match", "match_date", "league", "h", "a", "d", "home_goals", "away_goals"])
    monkeypatch.setattr(pd, "read_sql", fake)
    out = OC.run_output_checks(engine=object(), normalize=lambda t: t)
    assert not any(p.startswith("Resultados: no se pudo") for p in out)
    assert any("model_vs_sharp" in s and "skipped_at IS NULL" in s and "live_scores" not in s for s in seen)


# ─── el dashboard ────────────────────────────────────────────────────────────

def test_the_dashboard_says_postponed_not_live_zero_zero():
    from dashboard import board as B
    now = T("2026-11-08 21:00")
    pick = {"match": "estoril vs maritimo", "match_date": T("2026-11-08 15:30"), "league": "soccer_portugal_primeira_liga",
            "label": "Gana Estoril", "market": "home_win", "family": "Ganador", "reason": None, "model_prob": 0.5,
            "market_prob": 0.45, "result": None, "profit": None, "source": None, "playdoit_odds": 2.0}
    live = pd.DataFrame([{"home": "estoril", "away": "maritimo", "kickoff": T("2026-11-08 15:30"),
                          "home_goals": None, "away_goals": None, "home_corners": None, "away_corners": None,
                          "home_cards": None, "away_cards": None, "status": "postponed", "clock": "se pospuso"}])
    (r,) = B.past_matches(pd.DataFrame(), pd.DataFrame(), pd.DataFrame([pick]), pd.DataFrame(),
                          normalize=lambda t: str(t).lower(), now=now, live=live)
    assert r["score"] is None and r["live"] == {"status": "postponed", "clock": "se pospuso"}
    assert r["pick"]["result"] not in ("win", "loss")
    (item,) = B._day_items([r], r["date"], [])
    assert item["status"] == "postponed"
    js = open(B.__file__.replace("board.py", "ui_es5.js"), encoding="utf-8").read()
    assert "lv.status === 'postponed'" in js and "m.status === 'postponed'" in js and "x.live.status === 'postponed'" in js
    assert "se pospuso" in js
