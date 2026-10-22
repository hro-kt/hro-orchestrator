"""DB の列名。★実在しない列を参照していると、開催日の朝に初めて落ちる。

2026-10-22: ops_agent の主キーを `name` だと思って書いていた。実際は `server`。
このまま動かすと collect が例外で落ち、司令塔が何もしないまま一日が終わる。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_SRC = (Path(__file__).resolve().parents[1]
        / "hro_orchestrator" / "db.py").read_text(encoding="utf-8")
_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "hro-db" / "schema"


def _schema_text() -> str:
    if not _SCHEMA_DIR.is_dir():
        return ""
    return "\n".join(p.read_text(encoding="utf-8")
                     for p in sorted(_SCHEMA_DIR.glob("*.sql")))


def test_agent_table_uses_server_not_name():
    """★ops_agent の主キーは server。name という列は無い。"""
    assert "FROM ops_agent" in _SRC
    m = re.search(r"_SQL_AGENTS = \"\"\"(.*?)\"\"\"", _SRC, re.S)
    assert m
    sql = m.group(1)
    assert "server" in sql
    assert re.search(r"SELECT\s+name\b", sql) is None, "存在しない列 name を選んでいる"


def test_agent_server_matches_job_target_vocabulary():
    """★server の値が ops_job.target と同じ語彙("windows"/"vm")。

    ここが食い違うと agent を永久に見つけられず、
    「VM は立つのに投入されない」で一日を失う。
    """
    from hro_orchestrator.config import Config

    assert Config().policy.windows_target == "windows"


def test_every_column_we_select_exists_in_the_schema():
    """★スキーマと突き合わせる。手で書いた SQL は、実在しない列を平気で参照する。"""
    schema = _schema_text()
    if not schema:
        return                      # hro-db が隣に無い環境ではスキップ
    for table, cols in (("ops_agent", ("server", "last_seen")),
                        ("ops_job", ("kind", "args", "target", "status",
                                     "requested_at", "requested_by",
                                     "cancel_requested", "id")),
                        ("nl_ra", ("year", "month_day", "jyo_cd", "hasso_time"))):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in schema, table
        for c in cols:
            assert c in schema, f"{table}.{c} がスキーマに無い"
            assert c in _SRC or table == "nl_ra", f"{table}.{c} を使っていない"


def test_we_never_select_a_column_named_name():
    """今回の取り違えの再発防止。"""
    assert not re.search(r"SELECT\s+name\b", _SRC)


# --------------------------------------------------------------------------- #
# ★DB が落ちているときの出方
# --------------------------------------------------------------------------- #
def test_db_outage_is_explained_not_a_traceback(monkeypatch):
    """★2026-10-22 に PostgreSQL が保守で再起動し、agent が
         `FATAL: the database system is shutting down` を吐いた。
       司令塔は DB が無いと開催日かどうかすら分からない。
       トレースバックだけでは、保守なのか設定ミスなのか読み取れない。
    """
    from hro_orchestrator import db as dbmod
    from hro_orchestrator.config import Config

    monkeypatch.setattr(dbmod, "connect", lambda cfg: (_ for _ in ()).throw(
        RuntimeError("connection failed: FATAL:  the database system is shutting down")))
    try:
        dbmod.connect_or_explain(Config())
    except dbmod.DbUnavailable as e:
        msg = str(e)
    else:
        raise AssertionError("例外にならない")
    assert "DB に繋がりません" in msg
    assert "保守による再起動" in msg, "原因の見当を出していない"
    assert "死活監視" in msg, "外部監視が鳴ることを伝えていない"


def test_a_stopped_server_gets_a_different_hint(monkeypatch):
    from hro_orchestrator import db as dbmod
    from hro_orchestrator.config import Config

    monkeypatch.setattr(dbmod, "connect", lambda cfg: (_ for _ in ()).throw(
        OSError("timeout expired")))
    try:
        dbmod.connect_or_explain(Config())
    except dbmod.DbUnavailable as e:
        assert "停止されている" in str(e)
    else:
        raise AssertionError("例外にならない")


def test_every_command_guards_the_db():
    """★どのコマンドでもトレースバックを出さない。"""
    src = (Path(__file__).resolve().parents[1]
           / "hro_orchestrator" / "__main__.py").read_text(encoding="utf-8")
    for name in ("status_cmd", "plan_cmd", "run_cmd", "coldstart_cmd"):
        i = src.index(f"def {name}(")
        assert "@_guard_db" in src[max(0, i - 400):i], name
    assert "dbmod.connect(cfg)" not in src, "素の connect を呼んでいる"
