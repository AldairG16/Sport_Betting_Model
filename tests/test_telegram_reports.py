"""
Los mensajes del día en Telegram en palabras normales (6-nov-26).

El dueño: "cámbialo también" (el resumen de la noche, después del
dashboard). Y los tres mensajes leían bets_history, que dejó de llenarse el
25-sep-26: la mañana decía "No apuestes nada", la noche "Sin apuestas para
hoy" y el semanal "Sin apuestas jugadas esta semana", con 🎯 y parlays
recomendados en el dashboard. Ahora salen de program_picks y program_parlays.
"""

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

import src.models.telegram_reports as TR

MX = ZoneInfo("America/Mexico_City")
DAY = date(2026, 11, 7)
JARGON = ("ROI", "Profit", "unidades", "pasa la regla", "value bets", "Bets", "No apuestes nada",
          "datos en sombra", "WR:", "1u")


def _pick(match, hour, market="home_win", reason=None, result=None, profit=None, family="Ganador",
          league="soccer_germany_bundesliga", min_us=-250, odds=1.45, display=None, label=None, pa_paid=None):
    return {"match": match, "display": display, "match_date": pd.Timestamp(f"2026-11-07 {hour}", tz="UTC"),
            "league": league, "label": label or {"home_win": "Gana el local", "away_win": "Gana el visitante"}.get(
                market, "Menos de 9.5 córners"),
            "family": family, "market": market, "reason": reason, "min_us": min_us, "playdoit_odds": odds,
            "result": result, "profit": profit, "pa_paid": pa_paid}


def _leg(rank, match, odds, result=None, kind="dia", prob=0.75, slot="2026-11-07 14:30"):
    h, a = match.split(" vs ")
    return {"day": DAY, "slot": pd.Timestamp(slot, tz="UTC"), "rank": rank, "match": match,
            "match_date": pd.Timestamp(slot, tz="UTC"), "league": "soccer_germany_bundesliga",
            "display": f"{h.title()} vs {a.title()}", "market": "home_win", "label": f"Gana {h.title()}",
            "family": "Ganador", "model_prob": prob, "market_prob": prob, "safety": prob,
            "playdoit_odds": odds, "lineup": None, "rule": "bono", "kind": kind, "seq": 1, "pa": True,
            "pa_paid": None, "result": result, "profit": None}


def _plain(msg: str):
    for j in JARGON:
        assert j not in msg, j
    assert not re.search(r"\b1\.\d\d\b", msg), msg                     # ningún momio decimal


# ─── mañana ──────────────────────────────────────────────────────────────────

def test_the_morning_lists_the_targets_with_their_minimum_and_the_parlays():
    picks = [_pick("rb leipzig vs augsburg", "14:30", display="RB Leipzig vs Augsburg"),
             _pick("leeds united vs tottenham", "12:30", market="under25", reason="Sin ventaja: x",
                   label="Menos de 2.5 goles", family="Goles")]
    legs = [_leg(1, "dortmund vs elversberg", 1.28), _leg(2, "mainz vs bayern", 1.30, prob=0.73),
            _leg(3, "roma vs sassuolo", 1.28, prob=0.75), _leg(4, "valencia vs madrid", 1.26, prob=0.76)]
    msg, n = TR.morning_text(DAY, picks, legs, MX)
    assert n == 1
    assert "☀️ <b>Hoy, sábado 7 nov</b>" in msg
    assert "<b>08:30</b> RB Leipzig vs Augsburg\n   Gana RB Leipzig ⚡ · apuesta si PlayDoit paga <b>-250</b> o más" in msg
    assert "Parlay del día · 4 partidos · paga <b>+168</b> (con el bono: <b>+181</b>)" in msg
    assert "se gana más o menos 1 de cada 3 veces" in msg
    assert "🤖 Otro partido de hoy no tiene 🎯: sale en" in msg and "Leeds" not in msg
    _plain(msg)


def test_a_morning_without_targets_says_there_is_no_need_to_bet():
    msg, n = TR.morning_text(DAY, [_pick("a vs b", "15:00", reason="No es el muy favorito")], [], MX)
    assert n == 0
    assert "🎯 Hoy no hay recomendados" in msg and "No hace falta apostar" in msg
    assert "Parlays" not in msg
    _plain(msg)
    tomorrow, _ = TR.morning_text(DAY, [], [], MX, when="Mañana")
    assert tomorrow.startswith("🌙 <b>Mañana, sábado 7 nov</b>") and "Mañana no hay recomendados" in tomorrow


# ─── noche ───────────────────────────────────────────────────────────────────

NIGHT = datetime(2026, 11, 8, 3, 5, tzinfo=timezone.utc)


def test_the_evening_says_how_each_target_ended_and_the_gain_on_50():
    picks = [_pick("rb leipzig vs augsburg", "14:30", result="win", profit=0.45, pa_paid=True),
             _pick("mainz vs bayern", "14:30", market="away_win", result="loss", profit=-1.0, odds=1.3),
             _pick("toulouse vs strasbourg", "19:45", market="corners_under_9.5", reason="Sin ventaja: x",
                   family="Córners"),
             _pick("leeds vs spurs", "12:30", market="under25", reason="x", result="win", profit=0.85,
                   label="Menos de 2.5 goles", family="Goles")]
    legs = [_leg(1, "dortmund vs elversberg", 1.28, "win"), _leg(2, "mainz vs bayern", 1.30, "loss"),
            _leg(3, "roma vs sassuolo", 1.28, "win")]
    since = [{"market": "home_win", "reason": None, "result": r, "profit": p}
             for r, p in [("win", 0.4)] * 111 + [("loss", -1.0)] * 92]
    msg = TR.evening_text(DAY, picks, legs, since, {"stars": 11, "parlays": 3}, NIGHT, MX)
    assert "🌙 <b>Así terminó el sábado 7 nov</b>" in msg
    assert "🎯 <b>Recomendados</b>: 1 ganado y 1 perdido · apostando 50 a cada uno: <b>-27</b>" in msg
    assert "✅ Rb Leipzig vs Augsburg — Gana Rb Leipzig: ganado ⚡ (cobrado por ir 2 goles arriba) (+23)" in msg
    assert "❌ Mainz vs Bayern — Gana Bayern: perdido (-50)" in msg
    assert "❌ Parlay del día (3 partidos): perdido (2 de 3 ganados) · apostando 50: -50" in msg
    assert "🤖 Los demás picks (solo para seguirlos): 1 ganado y 0 perdidos · apostando 50 a cada uno: <b>+43</b>" in msg
    assert "📈 <b>Desde el 22 oct</b>, 🎯 recomendados: 111 de 203 ganados · apostando 50 a cada uno: <b>-2380</b>" in msg
    assert "🗓️ <b>Mañana</b>: 11 🎯 recomendados y 3 parlays" in msg
    assert "Toulouse" not in msg                                       # no es 🎯: va en la línea de los demás
    _plain(msg)


def test_a_pending_target_says_what_it_waits_for():
    corners = _pick("venezia vs udinese", "19:45", market="corners_under_9.5", family="Córners")
    late = _pick("necaxa vs tijuana", "02:00", market="home_win")
    late["match_date"] = pd.Timestamp("2026-11-08 02:00", tz="UTC")
    assert TR.pick_status(corners, NIGHT) == ("⏳", "esperando el dato oficial de córners (llega en 1 o 2 días)")
    assert TR.pick_status(late, NIGHT) == ("🕒", "se está jugando")
    assert TR.pick_status({**late, "match_date": pd.Timestamp("2026-11-08 05:00", tz="UTC")}, NIGHT)[1] == \
        "todavía no empieza"
    assert TR.pick_status({**late, "result": "push"}, NIGHT)[1] == "anulado: se devuelve lo apostado"
    msg = TR.evening_text(DAY, [corners], [], [], {}, NIGHT, MX)
    assert "🎯 <b>Recomendados</b>: todavía no termina ninguno" in msg
    assert "🗓️ <b>Mañana</b>: por ahora sin 🎯 recomendados." in msg and "Desde el 22 oct" not in msg


def test_a_won_parlay_shows_its_gain_with_the_bonus():
    legs = [_leg(i, f"t{i} vs u{i}", 1.3, "win") for i in range(1, 5)]
    msg = TR.evening_text(DAY, [], legs, [], {}, NIGHT, MX)
    # 1.3^4 = 2.8561 → ganancia 1.8561 × 1.075 de bono = 1.9953
    assert "✅ Parlay del día (4 partidos): ganado · apostando 50: <b>+100</b>" in msg
    assert "🎯 Hoy no hubo recomendados." in msg


# ─── semana ──────────────────────────────────────────────────────────────────

def test_the_weekly_report_counts_targets_parlays_and_the_rest():
    picks = ([_pick(f"a{i} vs b{i}", "14:30", result="win", profit=0.4) for i in range(3)]
             + [_pick("c vs d", "14:30", result="loss", profit=-1.0, league="soccer_spain_la_liga")]
             + [_pick("e vs f", "14:30", market="over25", reason="x", result="loss", profit=-1.0,
                      label="Más de 2.5 goles", family="Goles")])
    legs = [_leg(i, f"t{i} vs u{i}", 1.3, "win") for i in range(1, 5)]
    since = [{"market": "home_win", "reason": None, "result": "win", "profit": 0.4}] * 4
    lines = TR.weekly_lines(picks, legs, since)
    msg = "\n".join(lines)
    assert "🎯 Recomendados: 3 ganados y 1 perdido · apostando 50 a cada uno: <b>+10</b>" in msg
    assert "Mejor liga: Bundesliga (3 de 3, +60)" in msg and "Peor liga: LaLiga (0 de 1, -50)" in msg
    assert "🧩 Parlays: 1 ganado y 0 perdidos · apostando 50 a cada uno: <b>+100</b>" in msg
    assert "🤖 Los demás picks (solo para seguirlos): 0 ganados y 1 perdido" in msg
    assert "🎯 Recomendados: 4 de 4 ganados · apostando 50 a cada uno: <b>+80</b>" in msg
    _plain(msg)


def test_money_is_said_betting_50():
    # todo a 50 (el dueño, 8-nov-26), con el mismo redondeo que el dashboard
    assert TR.money(0.45) == "+23" and TR.money(-1) == "-50" and TR.money(0.001) == "0"
    assert TR.money(-0.55) == "-27" and TR.money(-0.25) == "-12" and TR.money(2.2419) == "+112"
    assert TR.money(None) == "0"


# ─── los envíos: ya no leen bets_history ─────────────────────────────────────

def _fake_db(monkeypatch, picks, legs):
    seen = []

    def fake(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        seen.append(s)
        if "program_parlays" in s:
            return pd.DataFrame(legs)
        return pd.DataFrame(picks)
    monkeypatch.setattr(TR.pd, "read_sql", fake)
    return seen


def test_the_three_messages_read_the_program_picks_not_bets_history(monkeypatch):
    import scripts.notify_telegram as nt
    from src.models import lineup_changes
    picks = [_pick("rb leipzig vs augsburg", "14:30", result="win", profit=0.45)]
    seen = _fake_db(monkeypatch, picks, [])
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    monkeypatch.setattr(nt, "_local_today", lambda: DAY)
    monkeypatch.setattr(nt, "PAPER_LOG_PATH", nt.Path("/nonexistent/paper.jsonl"))
    monkeypatch.setattr(lineup_changes, "weekly_lines", lambda engine: ["— <b>CAMBIOS CON LA ALINEACIÓN</b> —"])
    announced = []
    monkeypatch.setattr(nt, "_remember_announced", lambda day, stars: announced.append((day, stars)))
    nt.notify_best_bets()
    nt.send_evening_summary(target_date=DAY)
    nt.send_weekly_report()
    assert len(sent) == 3
    assert "Gana Rb Leipzig" in sent[0] and nt.get_last_picks_shown() == 1
    # v2.11.0: los 🎯 del mensaje quedan guardados para los picks finales
    assert [(d, [r["match"] for r in st]) for d, st in announced] == [(DAY, ["rb leipzig vs augsburg"])]
    assert "Así terminó el sábado 7 nov" in sent[1] and "ganado (+23)" in sent[1]
    assert "REPORTE DE LA SEMANA" in sent[2] and "CAMBIOS CON LA ALINEACIÓN" in sent[2]
    assert all("bets_history" not in s for s in seen) and any("program_picks" in s for s in seen)
    for m in sent:
        _plain(m)


def test_a_database_error_sends_nothing_instead_of_an_empty_summary(monkeypatch):
    import scripts.notify_telegram as nt

    def boom(*a, **k):
        raise RuntimeError("sin conexión")
    monkeypatch.setattr(TR.pd, "read_sql", boom)
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    monkeypatch.setattr(nt, "_remember_announced", lambda day, stars: sent.append("guardó"))
    nt.send_evening_summary(target_date=DAY)
    nt.notify_best_bets()
    nt.send_weekly_report()
    assert sent == []


def test_the_failure_notice_is_plain_and_no_longer_reads_bets_history(monkeypatch):
    import inspect
    import scripts.notify_telegram as nt
    import scripts.orchestrator as orch
    sent = []
    monkeypatch.setattr(nt, "send_message", lambda m: sent.append(m) or True)
    nt.send_health_check(mode="morning", picks_today=7, elapsed_seconds=618, steps_ok=40, steps_total=41)
    nt.send_health_check(mode="evening", picks_today=0, elapsed_seconds=95, steps_ok=30, steps_total=31)
    assert sent[0].startswith("⚠️ <b>La corrida de la mañana tuvo fallas</b>")
    assert "1 de 41 pasos fallaron" in sent[0] and "Recomendados en el mensaje de hoy: <b>7</b>" in sent[0]
    assert "Tardó: 10 min" in sent[0] and "Recomendados" not in sent[1] and "de la noche" in sent[1]
    for m in sent:
        _plain(m)
        assert "Bankroll" not in m and "Pending" not in m
    assert "FROM bets_history" not in inspect.getsource(nt.send_health_check)
    src = inspect.getsource(orch)
    start = src.index("# ── Health check: resumen de salud del ciclo")
    block = src[start:src.index("send_health_check(", start + 200)]
    assert "FROM bets_history" not in block and "get_last_picks_shown()" in block
