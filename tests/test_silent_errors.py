"""
Errores silenciosos (auditoría del 1-oct-26): fallas que se toleraban como
WARNING o print —y por eso nunca llegaban a Telegram— y trabajos que dejaban
de correr sin que nada avisara. Cada prueba fija que la falla ahora es un
ERROR (RUN_ISSUES → Telegram), deja la corrida en rojo o falla CERRADA.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine, FakeResult


@pytest.fixture(autouse=True)
def _clean_issues():
    RUN_ISSUES.reset()
    yield
    RUN_ISSUES.reset()


def _errors(text: str) -> list:
    return [e for e in RUN_ISSUES.errors() if text in e]


class _Broken:
    """Motor cuya conexión falla (DB caída, contraseña vencida)."""

    def begin(self):
        raise RuntimeError("could not connect to server")

    connect = begin


# ============================================================
# Estado aprendido (model_state y load_learned_state)
# ============================================================

def test_model_state_db_failures_are_errors(monkeypatch, tmp_path):
    import src.utils.model_state as ms
    monkeypatch.setattr(ms, "engine", _Broken())
    assert ms.save_state("anchor_weights", {"w": 1}) is False
    assert _errors("model_state/anchor_weights: no se pudo persistir")
    f = tmp_path / "x.json"
    f.write_text('{"w": 2}', encoding="utf-8")
    assert ms.load_state("anchor_weights", f) == {"w": 2}          # fallback local, pero avisado
    assert _errors("model_state/anchor_weights: DB no disponible")


def test_a_broken_learned_state_loader_is_recorded(monkeypatch):
    import scripts.clv_gate as cg
    import src.pipeline.prediction_pipeline as pp

    def boom():
        raise RuntimeError("DB caída")
    monkeypatch.setattr(cg, "load_clv_blocked_leagues", boom)
    st = pp.load_learned_state()
    assert st["blocked_leagues"] == set() and "blocked_leagues" in st["load_errors"]
    assert _errors("Estado aprendido 'blocked_leagues' no disponible")


def test_without_learned_state_no_real_bet_is_registered():
    """Falla CERRADA: con el default se levantaban en silencio los bloqueos
    de ligas y mercados, y el peso del modelo volvía al inicial."""
    from src.pipeline.prediction_pipeline import real_bets_with_state
    bets = [{"match": "a vs b"}, {"match": "c vs d"}]
    assert real_bets_with_state({"load_errors": ["anchor"]}, bets) == []
    assert _errors("2 apuestas reales NO registradas")
    RUN_ISSUES.reset()
    assert real_bets_with_state({"load_errors": []}, bets) == bets
    assert real_bets_with_state({}, bets) == bets
    assert RUN_ISSUES.errors() == []


# ============================================================
# Bankroll: una lectura fallida ya no es "100u"
# ============================================================

def test_bankroll_read_failure_raises_instead_of_returning_100(monkeypatch):
    import src.models.bankroll_manager as bm
    monkeypatch.setattr(bm, "ensure_bankroll_schema", lambda: None)

    def fail(*a, **k):
        raise RuntimeError("SSL connection has been closed unexpectedly")
    monkeypatch.setattr(bm.pd, "read_sql", fail)
    with pytest.raises(RuntimeError):
        bm.get_current_bankroll()
    assert _errors("No se pudo leer el bankroll")


def test_bankroll_empty_table_is_the_initial_bankroll(monkeypatch):
    import src.models.bankroll_manager as bm
    monkeypatch.setattr(bm, "ensure_bankroll_schema", lambda: None)
    monkeypatch.setattr(bm.pd, "read_sql", lambda *a, **k: pd.DataFrame(columns=["current_bankroll"]))
    assert bm.get_current_bankroll() == float(bm.INITIAL_BANKROLL)
    monkeypatch.setattr(bm.pd, "read_sql", lambda *a, **k: pd.DataFrame([{"current_bankroll": 59.47}]))
    assert bm.get_current_bankroll() == 59.47


# ============================================================
# Telegram: un mensaje que no llega deja la corrida en rojo
# ============================================================

class _Resp:
    def __init__(self, status):
        self.status_code, self.text, self.headers = status, "error", {}


def test_undelivered_telegram_message_is_recorded(monkeypatch):
    import time
    import scripts.notify_telegram as nt
    monkeypatch.setattr(nt, "SEND_FAILURES", [])
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(nt.requests, "post", lambda *a, **k: _Resp(401))
    assert nt._send_to_bot("hola", "token", "chat") is False
    assert len(nt.SEND_FAILURES) == 1 and _errors("mensaje no enviado tras 3 intentos")


def test_unconfigured_telegram_is_recorded(monkeypatch):
    import scripts.notify_telegram as nt
    monkeypatch.setattr(nt, "SEND_FAILURES", [])
    assert nt._send_to_bot("hola", "", "chat", "Telegram prekickoff") is False
    assert nt.SEND_FAILURES == ["Telegram prekickoff: sin configurar"]
    assert _errors("Telegram prekickoff no configurado")


def test_delivered_message_records_nothing(monkeypatch):
    import scripts.notify_telegram as nt
    monkeypatch.setattr(nt, "SEND_FAILURES", [])
    monkeypatch.setattr(nt.requests, "post", lambda *a, **k: _Resp(200))
    assert nt._send_to_bot("hola", "token", "chat") is True
    assert nt.SEND_FAILURES == [] and RUN_ISSUES.errors() == []


def test_orchestrator_sees_the_failed_deliveries(monkeypatch):
    import scripts.notify_telegram as nt
    import scripts.orchestrator as orch
    monkeypatch.setattr(nt, "SEND_FAILURES", ["Telegram: 'RESUMEN'"])
    assert orch._telegram_send_failures() == ["Telegram: 'RESUMEN'"]
    monkeypatch.setattr(nt, "SEND_FAILURES", [])
    assert orch._telegram_send_failures() == []


# ============================================================
# Closing y sombra: lo que mide el modo recolección
# ============================================================

def test_shadow_closing_failure_is_an_error(monkeypatch):
    import scripts.update_closing_odds as uco
    import src.models.save_bets as sb

    def boom():
        raise RuntimeError("relation shadow_bets does not exist")
    monkeypatch.setattr(sb, "_update_shadow_closing", boom)
    uco._close_shadow()
    assert _errors("shadow closing falló")


def test_rejected_shadow_candidates_are_one_error(monkeypatch):
    import src.models.save_bets as sb

    def respond(sql, params):
        if sql.startswith("INSERT INTO shadow_bets"):
            raise ValueError("invalid input syntax for type numeric")
        return FakeResult()
    monkeypatch.setattr(sb, "engine", FakeEngine(respond))
    recs = [{"match": f"a{i} vs b", "match_date": pd.Timestamp("2026-10-02"), "league": "x",
             "market": "over25", "p_final": 0.5, "p_ref": 0.5, "deviation": 0.0,
             "odds": 2.0, "edge_market": 0.0, "reason": "r"} for i in range(3)]
    sb.persist_shadow_bets(recs)
    (msg,) = _errors("shadow: 3 de 3 candidatas rechazadas")
    assert "invalid input syntax" in msg


def test_bets_that_end_without_result_are_one_error(monkeypatch):
    """La transición a 'stale' (terminal, fuera del bankroll) era un print:
    así pasaron 34 apuestas reales en abril-mayo sin que nadie se enterara."""
    import src.models.save_bets as sb

    def respond(sql, params):
        if sql.startswith("UPDATE bets_history SET result = 'stale'"):
            return FakeResult(rows=[("leeds united vs derby county", "over25")], rowcount=1)
        return FakeResult(rowcount=0)
    monkeypatch.setattr(sb, "engine", FakeEngine(respond))
    monkeypatch.setattr(sb, "ensure_bankroll_schema", lambda: None)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: pd.DataFrame())
    monkeypatch.setattr(sb, "plan_bet_settlements", lambda bets, matches: [])
    monkeypatch.setattr(sb.pd, "read_sql", lambda *a, **k: pd.DataFrame(
        [{"id": 1, "match": "leeds united vs derby county", "market": "over25",
          "result": "unresolved", "match_date": pd.Timestamp("2026-09-20", tz="UTC")}]))
    sb.update_bet_results()
    (msg,) = _errors("1 apuestas reales quedaron SIN RESULTADO")
    assert "leeds united vs derby county | over25" in msg


# ============================================================
# CLV gate: sin datos NO se desbloquea nada
# ============================================================

def _gate_world(monkeypatch, bets: pd.DataFrame, leagues: pd.DataFrame, prev: dict):
    import scripts.clv_gate as cg
    import src.utils.closing_quality as cq
    store = {k: dict(v) for k, v in prev.items()}

    def fake_read_sql(sql, con=None, params=None, **kw):
        return (leagues if "SELECT league, odds, closing_odds" in str(sql) else bets).copy()
    monkeypatch.setattr(cq, "ensure_closing_columns", lambda engine: None)
    monkeypatch.setattr(cg.pd, "read_sql", fake_read_sql)
    monkeypatch.setattr(cg, "load_state", lambda key, path=None: store.get(key))
    monkeypatch.setattr(cg, "save_state", lambda key, value, file_path=None: store.__setitem__(key, value))
    return cg, store


EMPTY_BETS = pd.DataFrame(columns=["market", "odds", "closing_odds"])
EMPTY_LEAGUES = pd.DataFrame(columns=["league", "odds", "closing_odds"])


def test_clv_gate_without_data_keeps_previous_blocks(monkeypatch):
    """Antes: lista vacía escrita (fallaba ABIERTO) y retorno antes del gate
    por liga, que quedó congelado desde el 22-sep."""
    import scripts.clv_gate as cg0
    prev = {cg0.STATE_KEY_MARKETS: {"blocked_markets": ["btts_no"], "negative_streak": {"btts_no": 3}},
            cg0.STATE_KEY_LEAGUES: {"blocked_leagues": ["soccer_spl"]}}
    cg, store = _gate_world(monkeypatch, EMPTY_BETS, EMPTY_LEAGUES, prev)
    res = cg.run_clv_gate(verbose=False)
    assert res["status"] == "no_data"
    assert store[cg.STATE_KEY_MARKETS]["blocked_markets"] == ["btts_no"]
    assert store[cg.STATE_KEY_LEAGUES]["blocked_leagues"] == ["soccer_spl"]


def test_league_gate_short_sample_keeps_a_block_and_failure_keeps_all(monkeypatch):
    import scripts.clv_gate as cg0
    prev = {cg0.STATE_KEY_LEAGUES: {"blocked_leagues": ["soccer_spl"]}}
    few = pd.DataFrame([{"league": "soccer_spl", "odds": 2.0, "closing_odds": 2.0}] * 3)
    cg, store = _gate_world(monkeypatch, EMPTY_BETS, few, prev)
    cg.run_clv_gate(verbose=False)
    assert store[cg.STATE_KEY_LEAGUES]["blocked_leagues"] == ["soccer_spl"]

    def fail(sql, con=None, params=None, **kw):
        if "SELECT league, odds, closing_odds" in str(sql):
            raise RuntimeError("timeout")
        return EMPTY_BETS.copy()
    monkeypatch.setattr(cg.pd, "read_sql", fail)
    cg.run_clv_gate(verbose=False)
    assert store[cg.STATE_KEY_LEAGUES]["blocked_leagues"] == ["soccer_spl"]
    assert _errors("League gate falló, se conservan los bloqueos anteriores")


# ============================================================
# Backtest: la racha de la alarma es la ACTUAL
# ============================================================

def _backtest(monkeypatch, capsys, results):
    import src.models.backtest_engine as be
    rows = [{"result": r, "odds": 2.0, "stake": 1.0, "edge": 0.05, "clv": None,
             "closing_fetched_at": None, "market": "over25", "league": "soccer_epl",
             "match_date": pd.Timestamp("2026-09-01") + pd.Timedelta(days=i)}
            for i, r in enumerate(results)]
    monkeypatch.setattr(be.pd, "read_sql", lambda *a, **k: pd.DataFrame(rows))
    be.run_backtest()
    return capsys.readouterr().out


def test_backtest_alarm_uses_the_current_streak(monkeypatch, capsys):
    out = _backtest(monkeypatch, capsys, ["loss"] * 10 + ["win"] * 3)
    assert "Racha max perdidas: 10 (histórica) · actual: 0" in out
    assert "Racha ACTUAL" not in out
    out = _backtest(monkeypatch, capsys, ["win"] * 3 + ["loss"] * 9)
    assert "Racha ACTUAL de 9 pérdidas consecutivas" in out


def test_backtest_clv_needs_valid_closings(monkeypatch, capsys):
    out = _backtest(monkeypatch, capsys, ["win", "loss"] * 5)
    assert "cierres válidos, n=0" in out and "Muestra insuficiente" in out
    assert "modelo funciona, continuar" not in out


# ============================================================
# Resolvedor de pendientes
# ============================================================

def test_resolver_can_include_real_bets_left_without_source(monkeypatch):
    import scripts.resolve_pending_bets as rp
    seen = []
    monkeypatch.setattr(rp.pd, "read_sql", lambda sql, *a, **k: seen.append(" ".join(str(sql).split()))
                        or pd.DataFrame())
    rp._fetch_pending_grouped(6, 15)
    rp._fetch_pending_grouped(6, 15, include_stale=True)
    assert "result = 'stale'" not in seen[0]
    assert "OR (result = 'stale' AND NOT" in seen[1]      # nunca las canceladas antes del kickoff


def test_resolver_without_api_key_fails_and_still_records_the_run(monkeypatch):
    import scripts.resolve_pending_bets as rp
    runs = []
    monkeypatch.setattr(rp, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(rp, "_record", lambda seconds, failed: runs.append(failed))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        rp.main()
    assert runs == [1]


def test_resolver_skips_low_confidence_and_counts_errors(monkeypatch):
    import anthropic
    import scripts.resolve_pending_bets as rp
    runs, upserts = [], []
    monkeypatch.setattr(rp, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(anthropic, "Anthropic", lambda api_key=None: object())
    monkeypatch.setattr(rp, "_record", lambda seconds, failed: runs.append(failed))
    monkeypatch.setattr(rp, "_fetch_pending_grouped", lambda **k: pd.DataFrame([
        {"match": "a vs b", "match_date": pd.Timestamp("2026-09-20"), "markets": ["over25"], "n_bets": 1},
        {"match": "c vs d", "match_date": pd.Timestamp("2026-09-20"), "markets": ["over25"], "n_bets": 1}]))
    monkeypatch.setattr(rp, "_which_fields_missing", lambda *a, **k: {"home_goals", "away_goals"})
    answers = iter([{"completed": True, "confidence": "low", "home_goals": 1, "away_goals": 0},
                    RuntimeError("overloaded")])

    def ask(*a, **k):
        x = next(answers)
        if isinstance(x, Exception):
            raise x
        return x
    monkeypatch.setattr(rp, "_ask_claude_for_result", ask)
    monkeypatch.setattr(rp, "_upsert_match_row", lambda *a, **k: upserts.append(a) or True)
    rp.main(silent_telegram=True)
    assert upserts == []                                   # confianza baja: no liquida dinero
    assert runs == [1] and _errors("1 consulta(s) a Claude fallaron")


JSON_OK = ('{"home_goals": 4, "away_goals": 1, "home_goals_ht": 3, "away_goals_ht": 1, '
           '"completed": true, "source_url": "https://www.sofascore.com/x", "confidence": "high", '
           '"notes": null}')


@pytest.mark.parametrize("text", [
    JSON_OK,
    "Excelente, tengo información confiable. Del análisis de las fuentes:\n\n" + JSON_OK,
    "```json\n" + JSON_OK + "\n```",
    JSON_OK.replace('"home_goals_ht": 3,', '"home_goals_ht": 3,   // al minuto 45\n'),
])
def test_resolver_reads_the_json_even_with_prose_or_comments(text):
    """9 de 10 respuestas pagadas se perdían el 2-oct-26 como "Parse error"."""
    from scripts.resolve_pending_bets import parse_result_json
    d = parse_result_json(text)
    assert (d["home_goals"], d["away_goals"], d["home_goals_ht"]) == (4, 1, 3)
    assert d["source_url"] == "https://www.sofascore.com/x"     # el // de la URL no es comentario


def test_resolver_without_json_is_an_error_not_a_print(monkeypatch):
    import src.utils.anthropic_budget as ab
    from types import SimpleNamespace
    import scripts.resolve_pending_bets as rp
    monkeypatch.setattr(ab, "can_call", lambda engine, cost: (True, ""))
    monkeypatch.setattr(ab, "record_call", lambda *a, **k: None)
    blocks = [SimpleNamespace(type="text", text="Busco el resultado. "),
              SimpleNamespace(type="server_tool_use"),
              SimpleNamespace(type="text", text=JSON_OK[:40]),       # la respuesta llega en
              SimpleNamespace(type="text", text=JSON_OK[40:])]       # varios bloques (citas)

    class Client:
        def __init__(self, content, stop="end_turn"):
            resp = SimpleNamespace(content=content, stop_reason=stop, usage=None)
            self.messages = SimpleNamespace(create=lambda **k: resp)
    assert rp._ask_claude_for_result(Client(blocks), "a", "b", "2026-04-10", {"home_goals"})["home_goals"] == 4
    with pytest.raises(ValueError, match="respuesta sin JSON válido.*stop_reason=max_tokens"):
        rp._ask_claude_for_result(Client([SimpleNamespace(type="text", text="Según FotMob, el partido")],
                                         stop="max_tokens"), "a", "b", "2026-04-10", {"home_goals"})


def test_standalone_resolver_reports_failed_queries():
    """Fuera del orquestador no hay reporte de errores tolerados: el
    resolvedor suelto avisa por Telegram y sale en rojo."""
    import scripts.resolve_pending_bets as rp
    sent = []
    assert rp.report_failures(0, sent.append) == 0 and sent == []
    rp.log.error("❌ resolve_pending: 2 consulta(s) a Claude fallaron — a vs b: Error: <overloaded>")
    assert rp.report_failures(2, sent.append) == 1
    assert "2 consulta(s) a Claude fallaron" in sent[0] and "&lt;overloaded&gt;" in sent[0]


# ============================================================
# Latido: el closing dispara los trabajos de horario fijo
# ============================================================

MX = timezone(timedelta(hours=-6))


def test_due_windows():
    from scripts.heartbeat_dispatch import due
    slots = ("07:00", "19:00")
    assert due(datetime(2026, 10, 1, 7, 0, tzinfo=MX), slots)
    assert due(datetime(2026, 10, 1, 7, 29, tzinfo=MX), slots)
    assert not due(datetime(2026, 10, 1, 7, 30, tzinfo=MX), slots)
    assert not due(datetime(2026, 10, 1, 6, 59, tzinfo=MX), slots)
    assert due(datetime(2026, 10, 1, 0, 10, tzinfo=MX), ("00:00",))


class _FakeGh:
    def __init__(self, last_created=None, fail_dispatch=False):
        self.calls, self.last_created, self.fail = [], last_created, fail_dispatch

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ("run", "list"):
            if self.last_created is None:
                return "[]"
            return f'[{{"createdAt": "{self.last_created:%Y-%m-%dT%H:%M:%SZ}"}}]'
        if args[:2] == ("workflow", "run") and self.fail:
            import subprocess
            raise subprocess.CalledProcessError(1, "gh", stderr="HTTP 403: Resource not accessible")
        return ""


def _dispatched(gh):
    return [a[2] for a in gh.calls if a[:2] == ("workflow", "run")]


def test_heartbeat_dispatches_only_due_jobs():
    from scripts.heartbeat_dispatch import main
    now = datetime(2026, 10, 1, 13, 5, tzinfo=timezone.utc)          # 07:05 en México
    gh = _FakeGh()
    assert main(now, gh) == 0
    assert _dispatched(gh) == ["resolve_pending.yml"]
    assert ("workflow", "run", "resolve_pending.yml", "--ref", "master") in gh.calls


def test_heartbeat_does_not_duplicate_a_recent_run():
    from scripts.heartbeat_dispatch import main
    now = datetime(2026, 10, 1, 13, 5, tzinfo=timezone.utc)
    gh = _FakeGh(last_created=now - timedelta(minutes=10))
    assert main(now, gh) == 0 and _dispatched(gh) == []
    gh = _FakeGh(last_created=now - timedelta(hours=12))
    main(now, gh)
    assert _dispatched(gh) == ["resolve_pending.yml"]


def test_heartbeat_failure_turns_the_closing_red(capsys):
    from scripts.heartbeat_dispatch import main
    now = datetime(2026, 10, 1, 15, 10, tzinfo=timezone.utc)          # 09:10: watchdog
    gh = _FakeGh(fail_dispatch=True)
    assert main(now, gh) == 1
    assert "::error::No se pudo disparar watchdog.yml" in capsys.readouterr().out


def test_heartbeat_outside_every_window_does_nothing():
    from scripts.heartbeat_dispatch import main
    gh = _FakeGh()
    assert main(datetime(2026, 10, 1, 17, 40, tzinfo=timezone.utc), gh) == 0     # 11:40
    assert gh.calls == []
