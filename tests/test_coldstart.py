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


def _run(alive_after=None, **kw):
    c = _Clock()
    calls = {"stop": [], "start": []}
    n = {"i": 0}

    def _alive(target):
        n["i"] += 1
        return alive_after is not None and n["i"] >= alive_after

    return run(vm="win1", target="windows",
               stop_vm=calls["stop"].append, start_vm=calls["start"].append,
               agent_alive=_alive, sleep=c.sleep, now=c.now,
               log=lambda m: None, **kw), calls


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
def test_timeout_explains_the_login_trigger_problem():
    res, _ = _run(alive_after=None, timeout_sec=60.0, poll_sec=15.0)
    assert not res.ok
    assert "ログイン契機" in res.problem
    assert "ログオンしているかどうかにかかわらず" in res.problem


def test_the_hint_mentions_the_user_account_requirement():
    """★JV-Link のサービスキーは HKCU、レシピはプロファイル配下。
       SYSTEM で走らせると見えない。
    """
    res, _ = _run(alive_after=None, timeout_sec=30.0, poll_sec=15.0)
    assert "HKCU" in res.problem and "実行ユーザ" in res.problem


def test_it_tells_the_operator_not_to_log_in():
    """★人が RDP すると、ログイン契機で上がってしまい**検証にならない**。"""
    res, _ = _run(alive_after=1)
    assert any("ログインしないでください" in s for s in res.steps)
