"""実行層。★判断はしない。plan が返したものをそのままやるだけ。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_orchestrator import runner as R  # noqa: E402
from hro_orchestrator.azure_vm import AzureVms  # noqa: E402
from hro_orchestrator.config import Config  # noqa: E402
from hro_orchestrator.plan import (ALERT, ENQUEUE, JST, START_VM, STOP_VM,  # noqa: E402
                                   WAIT, Action)


class _Az(AzureVms):
    def __init__(self, running=True, fail_read=False):
        super().__init__(resource_group="rg")
        self.started, self.stopped = [], []
        self._running, self._fail = running, fail_read

    def is_running(self, name):
        if self._fail:
            raise RuntimeError("az が応答しません")
        return self._running

    def state(self, name):
        if self._fail:
            raise RuntimeError("az が応答しません")
        return (self._running, False,
                "VM running" if self._running else "VM deallocated")

    def start(self, name):
        self.started.append(name)
        return True

    def stop(self, name):
        self.stopped.append(name)
        return True


def _cfg():
    return Config()


# --------------------------------------------------------------------------- #
# dry-run は**何も触らない**
# --------------------------------------------------------------------------- #
def test_dry_run_touches_nothing(monkeypatch):
    az = _Az()
    monkeypatch.setattr(R.dbmod, "enqueue_race_day",
                        lambda *a, **k: pytest.fail("dry-run で投入している"))
    out = R.execute(object(), az, _cfg(),
                    [Action(START_VM, "v", "r"), Action(STOP_VM, "v", "r"),
                     Action(ENQUEUE, "race_day", "r")], dry_run=True)
    assert az.started == [] and az.stopped == []
    assert len(out.done) == 3 and all(d.startswith("(dry-run)") for d in out.done)


def test_azure_dry_run_does_not_spawn_a_process():
    """★『実行したつもり』を型で防ぐ。dry_run ではプロセスを起こさない。"""
    az = AzureVms(resource_group="rg", dry_run=True)
    az.runner = lambda args: pytest.fail(f"dry-run で az を呼んでいる: {args}")
    assert az.start("v") is False
    assert az.stop("v") is False


def test_stop_uses_deallocate():
    """★`vm stop` だけでは課金が止まらない。"""
    seen = []
    az = AzureVms(resource_group="rg")
    az.runner = lambda args: seen.append(args) or "[]"
    az.stop("v")
    assert seen and seen[0][:2] == ["vm", "deallocate"]


# --------------------------------------------------------------------------- #
# 実行
# --------------------------------------------------------------------------- #
def test_actions_are_executed_in_order(monkeypatch):
    az = _Az()
    monkeypatch.setattr(R.dbmod, "enqueue_race_day", lambda *a, **k: 77)
    out = R.execute(object(), az, _cfg(),
                    [Action(START_VM, "v1", "r"), Action(ENQUEUE, "race_day", "r")])
    assert az.started == ["v1"]
    assert any("job#77" in d for d in out.done)


def test_wait_does_nothing():
    az = _Az()
    out = R.execute(object(), az, _cfg(), [Action(WAIT, "x", "まだ")])
    assert az.started == [] and az.stopped == [] and out.done == []


def test_alert_is_collected_and_sent():
    az = _Az()
    sent = []
    out = R.execute(object(), az, _cfg(), [Action(ALERT, "windows", "agent が死んでいます")],
                    alert=sent.append)
    assert out.alerts and not out.ok()
    assert sent and "agent が死んでいます" in sent[0]


def test_alert_is_not_sent_in_dry_run():
    sent = []
    R.execute(object(), _Az(), _cfg(), [Action(ALERT, "w", "x")],
              dry_run=True, alert=sent.append)
    assert sent == []


def test_unknown_action_is_an_alert_not_a_crash():
    """★知らない行動で開催日を落とさない。"""
    out = R.execute(object(), _Az(), _cfg(), [Action("fly_to_mars", "v", "r")])
    assert out.alerts and "未知の行動" in out.alerts[0]


# --------------------------------------------------------------------------- #
# ★電源状態が読めないときに「止める」へ倒さない
# --------------------------------------------------------------------------- #
def test_unreadable_power_state_is_treated_as_running(monkeypatch):
    """★読めないだけで、走っているジョブごと落としてはいけない。"""
    monkeypatch.setattr(R.dbmod, "posts", lambda c, d: (None, None, 0))
    monkeypatch.setattr(R.dbmod, "live_agents", lambda c, **k: {})
    monkeypatch.setattr(R.dbmod, "job_status", lambda c, d: {})
    monkeypatch.setattr(R.dbmod, "busy_by_target", lambda c: {})
    st = R.collect(object(), _Az(fail_read=True), _cfg(),
                   now=datetime(2026, 10, 24, 12, tzinfo=JST))
    v = st.vms[_cfg().policy.windows_vm]
    assert v.running is True
    assert "unknown" in v.power


def test_tick_is_idempotent(monkeypatch):
    """★timer から何度呼んでも安全。"""
    monkeypatch.setattr(R.dbmod, "posts", lambda c, d: (None, None, 0))
    monkeypatch.setattr(R.dbmod, "live_agents", lambda c, **k: {})
    monkeypatch.setattr(R.dbmod, "job_status", lambda c, d: {})
    monkeypatch.setattr(R.dbmod, "busy_by_target", lambda c: {})
    az = _Az(running=False)
    now = datetime(2026, 10, 24, 12, tzinfo=JST)
    a = R.tick(object(), az, _cfg(), now=now)
    b = R.tick(object(), az, _cfg(), now=now)
    assert a.actions == b.actions
    assert az.started == [] and az.stopped == []     # 非開催日・停止済み


# --------------------------------------------------------------------------- #
# ★判断を runner に書かない
# --------------------------------------------------------------------------- #
def test_execute_contains_no_policy():
    """★安全規則が2箇所に散ると、片方だけ直して事故る。

    collect は状態を集めるだけ、execute は言われたことをやるだけ。
    「いつ起こすか/いつ止めるか」の判断は plan にしか無いこと。
    """
    import re as _re

    src = (Path(__file__).resolve().parents[1]
           / "hro_orchestrator" / "runner.py").read_text(encoding="utf-8")
    m = _re.search(r"\ndef execute\(.*?\n(?=\ndef )", src, _re.S)
    assert m, "execute が見つからない"
    body = m.group(0)
    for word in ("first_post", "last_post", "is_race_day", "timedelta",
                 "start_lead", "stop_after", "enqueue_lead"):
        assert word not in body, f"execute が判断している: {word}"


def test_all_time_policy_lives_in_plan():
    """時刻の計算(いつ何をするか)は plan にしか無いこと。"""
    base = Path(__file__).resolve().parents[1] / "hro_orchestrator"
    plan_src = (base / "plan.py").read_text(encoding="utf-8")
    run_src = (base / "runner.py").read_text(encoding="utf-8")
    assert "timedelta(" in plan_src
    # runner が時刻演算を持たない(now() の取得だけは許す)
    assert "timedelta(" not in run_src, "runner に時刻の判断が漏れている"


# --------------------------------------------------------------------------- #
# ★遷移中(起動中/停止中)は何もしない
# --------------------------------------------------------------------------- #
class _AzTrans(AzureVms):
    def __init__(self, raw):
        super().__init__(resource_group="rg")
        self.raw = raw
        self.started, self.stopped = [], []

    def state(self, name):
        from hro_orchestrator.azure_vm import RUNNING, TRANSITIONAL
        return self.raw == RUNNING, self.raw in TRANSITIONAL, self.raw

    def start(self, name):
        self.started.append(name)

    def stop(self, name):
        self.stopped.append(name)


def test_a_transitioning_vm_is_left_alone(monkeypatch):
    """★起動中に起動を打ち直さない。

    2026-10-22: az vm start が既定で完了まで待ち、300秒で落ちた。
    --no-wait に変えた結果、次の周回では「起動中」が見える。
    そこを「止まっている」と読むと、10分ごとに起動を打ち続ける。
    """
    monkeypatch.setattr(R.dbmod, "posts", lambda c, d: (None, None, 0))
    monkeypatch.setattr(R.dbmod, "live_agents", lambda c, **k: {})
    monkeypatch.setattr(R.dbmod, "job_status", lambda c, d: {})
    monkeypatch.setattr(R.dbmod, "busy_by_target", lambda c: {})
    for raw in ("VM starting", "VM deallocating", "VM stopping"):
        az = _AzTrans(raw)
        out = R.tick(object(), az, _cfg(),
                     now=datetime(2026, 10, 22, 12, tzinfo=JST))
        assert az.started == [] and az.stopped == [], (raw, out.actions)
        assert any("遷移中" in a.reason for a in out.actions), raw


def test_start_and_stop_use_no_wait():
    """★az vm start は既定で完了まで待つ。Windows の冷間起動は数分かかり、
       subprocess のタイムアウトで落ちた(2026-10-22)。
       司令塔はポーリング型なので、投げたら次の周回で観測すればよい。
    """
    seen = []
    az = AzureVms(resource_group="rg")
    az.runner = lambda args: seen.append(args) or "[]"
    az.start("v")
    az.stop("v")
    assert "--no-wait" in seen[0] and "--no-wait" in seen[1]


def test_wait_for_power_polls_until_the_wanted_state():
    """待つのは az ではなく**こちら**。"""
    seq = ["VM deallocating", "VM deallocating", "VM deallocated"]
    az = AzureVms(resource_group="rg")
    az.power_state = lambda name: seq.pop(0)
    assert az.wait_for_power("v", "VM deallocated", poll_sec=0, sleep=lambda s: None)


def test_wait_for_power_gives_up():
    az = AzureVms(resource_group="rg")
    az.power_state = lambda name: "VM deallocating"
    assert az.wait_for_power("v", "VM deallocated", timeout_sec=0.0,
                             poll_sec=0, sleep=lambda s: None) is False
