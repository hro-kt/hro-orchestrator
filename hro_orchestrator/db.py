"""DB。状態を読むのと、ジョブを投入するだけ。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Config

JST = timezone(timedelta(hours=9))

# ★中央(JRA)のレースだけを見る。地方は nar_* にあり、電源の判断とは無関係。
_SQL_POSTS = """
SELECT min(hasso_time) AS first_post, max(hasso_time) AS last_post, count(*) AS n
  FROM nl_ra
 WHERE year = %s AND month_day = %s
   AND jyo_cd BETWEEN '01' AND '10'
   AND hasso_time ~ '^[0-9]{4}$'
"""

# ★kind ごとの**最新**の状態。再投入があるので最後の1本を見る。
_SQL_JOBS = """
SELECT DISTINCT ON (kind) kind, status
  FROM ops_job
 WHERE requested_at >= %s AND requested_at < %s
 ORDER BY kind, id DESC
"""

_SQL_BUSY = """
SELECT target, count(*)::int FROM ops_job
 WHERE status IN ('queued','running') AND NOT cancel_requested
 GROUP BY target
"""

# ★agent は pull 型。last_seen を自分で書き、こちらが古さを測る。
# ★主キーの列名は **server**(name ではない)。そしてその値が ops_job.target と
#   同じ語彙("windows" / "vm")。ここを取り違えると agent を永久に見つけられず、
#   「VM は立つのに投入されない」で一日を失う。
_SQL_AGENTS = """
SELECT server, EXTRACT(EPOCH FROM (now() - last_seen))::int AS age
  FROM ops_agent
"""


def connect(cfg: Config):
    pg = cfg.postgres
    try:
        import psycopg
        dsn = (f"host={pg.host} port={pg.port} dbname={pg.dbname} "
               f"user={pg.user} password={pg.password}")
        if pg.sslmode:
            dsn += f" sslmode={pg.sslmode}"
        return psycopg.connect(dsn, autocommit=True)
    except ModuleNotFoundError:
        pass
    import pg8000.dbapi
    kw: dict[str, Any] = dict(host=pg.host, port=pg.port, database=pg.dbname,
                              user=pg.user, password=pg.password)
    if pg.sslmode:
        import ssl
        kw["ssl_context"] = ssl.create_default_context()
    c = pg8000.dbapi.connect(**kw)
    c.autocommit = True
    return c


def _rows(conn, sql: str, params=()) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        return [tuple(r) for r in cur.fetchall()]
    finally:
        cur.close()


def _hhmm(date: str, v) -> datetime | None:
    s = str(v or "")
    if len(s) != 4 or not s.isdigit():
        return None
    return datetime(int(date[:4]), int(date[4:6]), int(date[6:8]),
                    int(s[:2]), int(s[2:]), tzinfo=JST)


def posts(conn, date: str) -> tuple[datetime | None, datetime | None, int]:
    r = _rows(conn, _SQL_POSTS, (date[:4], date[4:8]))
    if not r:
        return None, None, 0
    first, last, n = r[0]
    return _hhmm(date, first), _hhmm(date, last), int(n or 0)


def job_status(conn, date: str) -> dict[str, str | None]:
    """その日(JST)に投入されたジョブの、kind ごとの最新状態。"""
    start = datetime(int(date[:4]), int(date[4:6]), int(date[6:8]), tzinfo=JST)
    return {k: v for k, v in _rows(conn, _SQL_JOBS, (start, start + timedelta(days=1)))}


def busy_by_target(conn) -> dict[str, int]:
    return {t: n for t, n in _rows(conn, _SQL_BUSY)}


def live_agents(conn, *, max_age_sec: int = 60) -> dict[str, bool]:
    """{target: 生きているか}。★ops_agent.server が ops_job.target と同じ語彙。"""
    out: dict[str, bool] = {}
    for server, age in _rows(conn, _SQL_AGENTS):
        out[str(server)] = age is not None and int(age) <= max_age_sec
    return out


def enqueue_race_day(conn, date: str, flow_args_json: str, *,
                     requested_by: str = "hro-orchestrator") -> int:
    """race_day を投入する。args は UI から渡しているものと同じ形。"""
    import json as _json

    args: dict[str, Any] = {"date": date}
    if flow_args_json:
        try:
            args["flow_args"] = _json.loads(flow_args_json)
        except _json.JSONDecodeError as e:
            raise ValueError(f"ORC_FLOW_ARGS が JSON として読めません: {e}") from e
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO ops_job(kind,args,target,requested_by)"
            " VALUES('race_day', %s, 'vm', %s) RETURNING id",
            (_json.dumps(args, ensure_ascii=False), requested_by))
        return int(cur.fetchone()[0])
    finally:
        cur.close()
