"""
tests/test_db_backup.py
=======================
El respaldo semanal de la base en la PC del dueño (11-nov-26). El 9-nov Neon
bloqueó la base y no había ninguna copia: todo quedó atrapado.
"""

import json
from datetime import date, datetime, timezone

from src.utils import db_backup as BK


def test_what_goes_in_each_copy():
    tables = ["program_picks", "shadow_bets", "odds_history", "historical_odds", "matches", "match_events",
              "matches_identity_backup", "backup_rescheduled_20261027", "kalshi_history_backup", "model_state"]
    weekly = BK.plan_tables(tables, full=False)
    assert weekly == ["model_state", "odds_history", "program_picks", "shadow_bets"]
    full = BK.plan_tables(tables, full=True)
    assert "matches" in full and "match_events" in full
    assert "historical_odds" not in full and not any("backup" in t for t in full)


def test_when_a_copy_is_due():
    today = date(2026, 12, 20)
    assert BK.due([], today) == (True, True)
    assert BK.due([(date(2026, 12, 17), False), (date(2026, 12, 10), True)], today) == (False, False)
    assert BK.due([(date(2026, 12, 12), False), (date(2026, 12, 10), True)], today) == (True, False)
    assert BK.due([(date(2026, 12, 18), False), (date(2026, 11, 20), True)], today) == (True, True)


def test_a_half_written_copy_never_counts(tmp_path):
    (tmp_path / "2026-12-01.tmp").mkdir()
    (tmp_path / "2026-12-01.tmp" / "manifest.json").write_text(json.dumps({"date": "2026-12-01", "full": True}))
    (tmp_path / "2026-12-02").mkdir()                               # sin manifest: se cortó
    (tmp_path / "2026-12-03").mkdir()
    (tmp_path / "2026-12-03" / "manifest.json").write_text(json.dumps({"date": "2026-12-03", "full": False}))
    assert BK.existing(tmp_path) == [(date(2026, 12, 3), False)]


class _Conn:
    def __init__(self, answers):
        self.answers = answers

    def execute(self, sql, params=None):
        s = str(sql)
        for k, v in self.answers.items():
            if k in s:
                return type("R", (), {"fetchall": lambda self, v=v: v})()
        raise AssertionError(s[:60])


def test_the_schema_rebuilds_tables_constraints_indexes_and_sequences():
    conn = _Conn({                                   # la de índices nombra pg_constraint: va primero
        "FROM pg_indexes": [("idx_pipeline_runs_mode_ran",
                             "CREATE INDEX idx_pipeline_runs_mode_ran ON public.pipeline_runs USING btree (mode, ran_at DESC)")],
        "FROM pg_attribute": [("id", "integer", True, "nextval('public.pipeline_runs_id_seq'::regclass)", ""),
                              ("mode", "text", True, None, ""),
                              ("ran_at", "timestamp with time zone", True, "now()", "")],
        "FROM pg_constraint": [("pipeline_runs_pkey", "p", "PRIMARY KEY (id)")],
    })
    s = BK.table_schema(conn, "pipeline_runs")
    assert s["create"].startswith('CREATE TABLE "pipeline_runs" (')
    assert "\"id\" integer DEFAULT nextval('pipeline_runs_id_seq'::regclass) NOT NULL" in s["create"]
    assert '"ran_at" timestamp with time zone DEFAULT now() NOT NULL' in s["create"]
    assert s["constraints"] == [{"name": "pipeline_runs_pkey", "type": "p",
                                 "sql": 'ALTER TABLE "pipeline_runs" ADD CONSTRAINT "pipeline_runs_pkey" PRIMARY KEY (id)'}]
    assert s["indexes"] == ["CREATE INDEX idx_pipeline_runs_mode_ran ON pipeline_runs USING btree (mode, ran_at DESC)"]
    assert s["sequences"] == [{"column": "id", "sequence": "pipeline_runs_id_seq"}]


def test_the_daily_check_wants_a_recent_copy():
    now = datetime(2026, 12, 20, 12, tzinfo=timezone.utc)
    assert "todavía no hay ninguna copia" in BK.backup_late(None, now)
    assert BK.backup_late(datetime(2026, 12, 14, tzinfo=timezone.utc), now) is None
    assert "hace 16 días" in BK.backup_late(datetime(2026, 12, 5, tzinfo=timezone.utc), now)


def test_the_dashboard_starts_the_weekly_copy():
    from pathlib import Path
    src = (Path(__file__).parent.parent / "dashboard" / "entry.py").read_text(encoding="utf-8")
    assert "from src.utils.db_backup import start_thread" in src and "start_thread(engine)" in src
    gi = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "respaldos/" in gi
