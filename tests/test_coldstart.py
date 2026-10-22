"""冷間起動の検証。

★`az vm start` は Windows を起こすが**誰もログインしない**。
  agent がログイン契機で起動する設定のままだと、VM は立つのに agent は上がらない。
  司令塔は「応答しません」と鳴らすだけで、自動化として成立しない。
  開催日に初めて気づくのが最悪なので、非開催日に確かめられるようにする。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_orchestrator.coldstart import refuse_reason, run  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(alive_after=None, stale_heartbeat=False, **kw):
    """alive_after: 何回目の確認で**新しい**心拍が出るか(None なら永久に出ない)
       stale_heartbeat: 起動前の古い心拍が残っている状況(=偽陽性の元)
    """
    c = _Clock()
    calls = {"stop": [], "start": []}
    n = {"i": 0}
    mark = {"t": None}

    def _ref_now():
        mark["t"] = c.t
        return c.t

    def _seen(target):
        n["i"] += 1
        if stale_heartbeat and (alive_after is None or n["i"] < alive_after):
            # ★VM を落とした直後に残っている古い心拍。これで合格させてはいけない
            return (mark["t"] or 0) - 1
        if alive_after is not None and n["i"] >= alive_after:
            return (mark["t"] or 0) + 1
        return None

    kw.setdefault("wait_stopped", lambda v: True)
    return run(vm="win1", target="windows",
               stop_vm=calls["stop"].append, start_vm=calls["start"].append,
               agent_seen_at=_seen, ref_now=_ref_now,
               sleep=c.sleep, now=c.now, log=lambda m: None, **kw), calls


# --------------------------------------------------------------------------- #
# ★開催日には走らせない
# --------------------------------------------------------------------------- #
def test_refuses_on_a_race_day():
    """VM を止めるので、開催日にやるとその日が終わる。"""
    assert "開催日" in (refuse_reason(is_race_day=True, busy=0) or "")


def test_refuses_when_jobs_are_running():
    assert "ジョブ" in (refuse_reason(is_race_day=False, busy=2) or "")


def test_allows_on_an_idle_non_race_day():
    assert refuse_reason(is_race_day=False, busy=0) is None


# --------------------------------------------------------------------------- #
# 手順
# --------------------------------------------------------------------------- #
def test_stops_then_starts():
    res, calls = _run(alive_after=1)
    assert calls["stop"] == ["win1"] and calls["start"] == ["win1"]
    assert res.ok


def test_skip_stop_only_starts():
    """既に止まっているなら、止める必要はない。"""
    res, calls = _run(alive_after=1, skip_stop=True)
    assert calls["stop"] == [] and calls["start"] == ["win1"]


def test_success_reports_how_long_it_took():
    res, _ = _run(alive_after=4, poll_sec=10.0)
    assert res.ok and res.elapsed_sec > 0


# --------------------------------------------------------------------------- #
# ★失敗したときに「何をすればよいか」まで言う
# --------------------------------------------------------------------------- #
def test_timeout_points_at_auto_logon_not_session_zero():
    """★「ログオンしていなくても実行」は**使えない**。

    それは Session 0(非対話)で走るので JV-Link が動かない
    (対話的デスクトップを要求する。2026-09 確認)。
    誤った手順を出すと、開催日に JV-Link が全滅する。
    """
    res, _ = _run(alive_after=None, timeout_sec=60.0, poll_sec=15.0)
    assert not res.ok
    assert "自動ログオン" in res.problem
    assert "Session 0" in res.problem and "JV-Link が動かない" in res.problem


def test_the_hint_does_not_recommend_the_broken_approach():
    """★間違った助言への逆戻りを防ぐ。"""
    res, _ = _run(alive_after=None, timeout_sec=30.0, poll_sec=15.0)
    bad = "ログオンしているかどうかにかかわらず実行"
    assert bad not in res.problem, "使えない手順を勧めている"


def test_the_hint_mentions_the_user_account_requirement():
    """★JV-Link のサービスキーは HKCU、レシピ/state はプロファイル配下。
       別のユーザでログオンさせると、上がっても使えない。
    """
    res, _ = _run(alive_after=None, timeout_sec=30.0, poll_sec=15.0)
    assert "HKCU" in res.problem
    assert "いま運用しているユーザ" in res.problem


def test_it_tells_the_operator_not_to_log_in():
    """★人が RDP すると、ログイン契機で上がってしまい**検証にならない**。"""
    res, _ = _run(alive_after=1)
    assert any("ログインしないでください" in s for s in res.steps)


# --------------------------------------------------------------------------- #
# ★偽陽性: 起動前の古い心拍で合格してはいけない
# --------------------------------------------------------------------------- #
def test_a_stale_heartbeat_does_not_count_as_success():
    """★2026-10-23 の実害。VM を落とした直後は心拍がまだ新しく、
       「60秒以内なら生存」で見ていたため **0秒で合格**した。
       何も検証していないのに通ってしまう、最悪の壊れ方。
    """
    res, _ = _run(alive_after=None, stale_heartbeat=True,
                  timeout_sec=60.0, poll_sec=15.0)
    assert not res.ok, "古い心拍で合格している"


def test_a_heartbeat_after_the_start_counts():
    res, _ = _run(alive_after=3, stale_heartbeat=True, poll_sec=10.0)
    assert res.ok
    assert res.elapsed_sec > 0, "0秒で合格するのは検証できていない証拠"


def test_the_reference_time_comes_from_the_caller_not_the_local_clock():
    """★DB の last_seen と比べるので、基準も DB 側の時刻でなければならない。"""
    import re as _re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1]
           / "hro_orchestrator" / "coldstart.py").read_text(encoding="utf-8")
    assert "ref_now" in src and "mark = ref_now()" in src
    assert "seen > mark" in src
    assert "agent_alive" not in src, "生死で判定する旧実装が残っている"


def test_it_waits_for_the_stop_to_complete_before_starting():
    """★遷移中に起動を投げると弾かれる。停止/起動は --no-wait なので、
       待つのはこちらの役目。
    """
    order = []
    c = _Clock()
    res = run(vm="win1", target="windows",
              stop_vm=lambda v: order.append("stop"),
              start_vm=lambda v: order.append("start"),
              wait_stopped=lambda v: order.append("waited") or True,
              agent_seen_at=lambda t: 1.0, ref_now=lambda: 0.0,
              sleep=c.sleep, now=c.now, log=lambda m: None)
    assert res.ok
    assert order == ["stop", "waited", "start"]


def test_it_gives_up_if_the_vm_never_stops():
    c = _Clock()
    res = run(vm="win1", target="windows",
              stop_vm=lambda v: None, start_vm=lambda v: (_ for _ in ()).throw(
                  AssertionError("停止しきっていないのに起動している")),
              wait_stopped=lambda v: False,
              agent_seen_at=lambda t: 1.0, ref_now=lambda: 0.0,
              sleep=c.sleep, now=c.now, log=lambda m: None)
    assert not res.ok and "停止しきりません" in res.problem
