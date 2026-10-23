"""計画の安全規則。

★電源管理は「一日を丸ごと失う」新しい経路を増やす。VM が起動しなければ
  その日は何も買えない。だから判断は全部ここ(純粋関数)に置いてテストする。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_orchestrator.plan import (  # noqa: E402
    ALERT, ENQUEUE, JST, START_VM, STOP_VM, WAIT,
    DayState, Policy, VmState, plan,
)

POL = Policy()
WIN = POL.windows_vm


def _at(h, m=0):
    return datetime(2026, 10, 24, h, m, tzinfo=JST)


def _state(now, *, race_day=True, running=False, agent=False,
           jobs=None, busy=None, first=10, last=16, tomorrow=36):
    return DayState(
        now=now, date="20261024", is_race_day=race_day,
        first_post=_at(first) if race_day else None,
        last_post=_at(last) if race_day else None,
        vms={WIN: VmState(WIN, running)},
        agents={"windows": agent},
        jobs=jobs or {}, busy=busy or {}, tomorrow_races=tomorrow)


def kinds(acts):
    return [a.kind for a in acts]


# --------------------------------------------------------------------------- #
# 起こす
# --------------------------------------------------------------------------- #
def test_starts_the_vm_with_enough_lead():
    """★人が手で起こせる余裕を残す。起動に失敗した日は気づくのが遅いと丸ごと失う。"""
    acts = plan(_state(_at(7)), POL)          # 第1発走10:00 の3時間前
    assert kinds(acts) == [START_VM]
    assert acts[0].target == WIN


def test_does_nothing_too_early():
    acts = plan(_state(_at(5)), POL)
    assert kinds(acts) == [WAIT]
    assert "07:00" in acts[0].reason


def test_does_not_start_twice():
    acts = plan(_state(_at(7), running=True), POL)
    assert START_VM not in kinds(acts)


# --------------------------------------------------------------------------- #
# ★「VM が起動した」≠「agent が動いている」
# --------------------------------------------------------------------------- #
def test_waits_for_the_agent_before_enqueueing():
    """ここを飛ばすと、誰も拾わないジョブを投入して『正常に見えるが0件』になる。"""
    acts = plan(_state(_at(7, 5), running=True, agent=False), POL)
    assert kinds(acts) == [WAIT]
    assert ENQUEUE not in kinds(acts)


def test_alerts_when_the_agent_never_comes_up():
    acts = plan(_state(_at(7, 25), running=True, agent=False), POL)
    assert kinds(acts) == [ALERT]
    assert "agent が応答しません" in acts[0].reason


def test_the_alert_comes_while_there_is_still_time():
    """★第1発走より前に鳴ること。終わってから鳴っても取り返せない。"""
    st = _state(_at(7, 25), running=True, agent=False)
    acts = plan(st, POL)
    assert acts[0].kind == ALERT
    assert st.now < st.first_post


# --------------------------------------------------------------------------- #
# 投入する
# --------------------------------------------------------------------------- #
def test_enqueues_race_day_at_the_right_time():
    acts = plan(_state(_at(8), running=True, agent=True), POL)
    assert ENQUEUE in kinds(acts)


def test_does_not_enqueue_before_the_window():
    acts = plan(_state(_at(7, 5), running=True, agent=True), POL)
    assert ENQUEUE not in kinds(acts)
    assert WAIT in kinds(acts)


def test_does_not_enqueue_a_second_orchestrator():
    """★2026-10-17 に親が2本走って同じレースを2回買いかけた。"""
    for status in ("queued", "running"):
        acts = plan(_state(_at(9), running=True, agent=True,
                           jobs={"race_day": status}), POL)
        assert ENQUEUE not in kinds(acts), status


def test_a_failed_orchestrator_is_not_silently_restarted():
    """★同じ理由で落ちるなら何度入れ直しても同じ。人に見せる。"""
    acts = plan(_state(_at(9), running=True, agent=True,
                       jobs={"race_day": "failed"}), POL)
    assert ALERT in kinds(acts)
    assert ENQUEUE not in kinds(acts)


# --------------------------------------------------------------------------- #
# 止める。★ここが一番危ない
# --------------------------------------------------------------------------- #
def test_does_not_stop_before_the_last_race():
    acts = plan(_state(_at(15), running=True, agent=True,
                       jobs={"race_day": "running"}), POL)
    assert STOP_VM not in kinds(acts)


def test_does_not_stop_until_the_close_is_done():
    """★close_day は IPAT の投票履歴を読む。遡れるのは当日と前日だけ。

    ここで電源を切ると、当日の確定値を永久に失う。
    """
    acts = plan(_state(_at(17), running=True, agent=True,
                       jobs={"race_day": "done", "close_day": "running",
                             "tax_seal": None}), POL)
    assert STOP_VM not in kinds(acts)
    assert any("締めが済むまで止めません" in a.reason for a in acts)


def test_does_not_stop_until_the_seal_is_done():
    acts = plan(_state(_at(17), running=True, agent=True,
                       jobs={"race_day": "done", "close_day": "done",
                             "tax_seal": "running"}), POL)
    assert STOP_VM not in kinds(acts)


def test_does_not_stop_while_any_job_is_running_on_that_machine():
    acts = plan(_state(_at(17), running=True, agent=True,
                       jobs={"race_day": "done", "close_day": "done",
                             "tax_seal": "done"},
                       busy={"windows": 1}), POL)
    assert STOP_VM not in kinds(acts)
    assert any("走っています" in a.reason for a in acts)


def test_stops_when_everything_is_finished():
    acts = plan(_state(_at(17), running=True, agent=True,
                       jobs={"race_day": "done", "close_day": "done",
                             "tax_seal": "done"}), POL)
    assert STOP_VM in kinds(acts)


def test_stop_needs_the_margin_after_the_last_post():
    """最終発走の直後に切らない(投票のリトライや書き戻しが途中で切れる)。"""
    acts = plan(_state(_at(16, 10), running=True, agent=True,
                       jobs={"race_day": "done", "close_day": "done",
                             "tax_seal": "done"}), POL)
    assert STOP_VM not in kinds(acts)


# --------------------------------------------------------------------------- #
# 非開催日
# --------------------------------------------------------------------------- #
def test_stops_on_a_non_race_day():
    acts = plan(_state(_at(12), race_day=False, running=True), POL)
    assert kinds(acts) == [STOP_VM]


def test_already_stopped_is_a_no_op():
    assert plan(_state(_at(12), race_day=False, running=False), POL) == []


def test_does_not_stop_a_busy_machine_on_a_non_race_day():
    """★後日の決済や同期が回っていることがある。"""
    acts = plan(_state(_at(12), race_day=False, running=True,
                       busy={"windows": 2}), POL)
    assert STOP_VM not in kinds(acts)


def test_missing_post_time_is_an_alert_not_a_guess():
    st = _state(_at(7))
    st.first_post = None
    acts = plan(st, POL)
    assert kinds(acts) == [ALERT]
    assert "発走時刻が読めません" in acts[0].reason


# --------------------------------------------------------------------------- #
# 冪等
# --------------------------------------------------------------------------- #
def test_planning_is_idempotent():
    """★何度走らせても同じ答え。状態を持たない。"""
    st = _state(_at(8), running=True, agent=True)
    assert plan(st, POL) == plan(st, POL)


# --------------------------------------------------------------------------- #
# ★非開催日でも、仕事があるなら起こす
# --------------------------------------------------------------------------- #
def test_starts_the_vm_for_a_queued_job_on_a_non_race_day():
    """★止めるだけでは片手落ち。

    VM が止まっている間に admin から Windows 宛のジョブ(sync_all / settle /
    close_day 等)を投入すると、拾う agent が居らず**永久に queued のまま**になる。
    「誰も気づかないまま何も起きない」は、この系で一番高くつく壊れ方。
    """
    acts = plan(_state(_at(12), race_day=False, running=False,
                       busy={"windows": 1}), POL)
    assert kinds(acts) == [START_VM]
    assert "待っています" in acts[0].reason


def test_an_idle_stopped_vm_stays_stopped():
    acts = plan(_state(_at(12), race_day=False, running=False), POL)
    assert acts == []


def test_jobs_for_another_machine_do_not_wake_windows():
    """★vm 宛のジョブで Windows を起こさない(課金が無駄になる)。"""
    acts = plan(_state(_at(12), race_day=False, running=False,
                       busy={"vm": 3}), POL)
    assert acts == []


# --------------------------------------------------------------------------- #
# ★前日に「RACE データが古い」と気づく
# --------------------------------------------------------------------------- #
def test_warns_when_tomorrow_is_a_weekend_but_nl_ra_is_empty():
    """★司令塔は nl_ra を見て開催日を判断する。古いままだと
       「今日は非開催日」として**VM を起こさず、一日が静かに終わる**。
       fetch_race は JV-Link(Windows VM)が要るので、当日では遅い。
    """
    # 2026-10-24 は金曜。翌日は土曜
    acts = plan(_state(_at(12), race_day=False, running=False, tomorrow=0), POL)
    assert ALERT in kinds(acts)
    a = [x for x in acts if x.kind == ALERT][0]
    assert "nl_ra にレースがありません" in a.reason
    assert "fetch_race" in a.reason


def test_no_warning_when_tomorrow_has_races():
    acts = plan(_state(_at(12), race_day=False, running=False, tomorrow=36), POL)
    assert ALERT not in kinds(acts)


def test_no_warning_on_weekdays():
    """★JRA の出走表は週後半に配信される。月〜水に将来のレースが無いのは正常。"""
    st = _state(_at(12), race_day=False, running=False, tomorrow=0)
    st.now = datetime(2026, 10, 20, 12, tzinfo=JST)     # 月曜 → 翌日は火曜
    assert ALERT not in kinds(plan(st, POL))


def test_the_warning_does_not_block_stopping_the_vm():
    """★鳴らすのと止めるのは別。警告で電源管理を止めない。"""
    acts = plan(_state(_at(12), race_day=False, running=True, tomorrow=0), POL)
    assert ALERT in kinds(acts) and STOP_VM in kinds(acts)


def test_waking_and_stopping_never_happen_together():
    """★同じ周回で起こして止めると、永久に往復する。"""
    for busy in ({}, {"windows": 1}, {"vm": 2}):
        for running in (True, False):
            acts = plan(_state(_at(12), race_day=False, running=running,
                               busy=busy), POL)
            ks = kinds(acts)
            assert not (START_VM in ks and STOP_VM in ks), (busy, running)
