"""通知。★司令塔自身が落ちたことは、司令塔には報告できない。

VM が止まる / ディスクが埋まる / systemd が止まる。どれも「静かに何も起きない」
形になり、気づくのは夕方になる。それは今まで潰してきた失敗の形そのもの。
だから**外部の死活監視**を置く。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hro_orchestrator import notify  # noqa: E402
from hro_orchestrator import runner as R  # noqa: E402
from hro_orchestrator.azure_vm import AzureVms  # noqa: E402
from hro_orchestrator.config import Config  # noqa: E402
from hro_orchestrator.plan import ALERT, JST, WAIT, Action, DayState  # noqa: E402


class _Az(AzureVms):
    def __init__(self):
        super().__init__(resource_group="rg")

    def is_running(self, name):
        return True

    def state(self, name):
        return True, False, "VM running"


def _state(race_day=True):
    return DayState(now=datetime(2026, 10, 24, 12, tzinfo=JST), date="20261024",
                    is_race_day=race_day, first_post=None, last_post=None)


# --------------------------------------------------------------------------- #
# 送らない条件
# --------------------------------------------------------------------------- #
def test_no_url_means_no_call(monkeypatch):
    monkeypatch.setattr(notify, "_post",
                        lambda *a: (_ for _ in ()).throw(AssertionError("呼んではいけない")))
    assert notify.heartbeat("") is False
    assert notify.alert("", "x") is False
    assert notify.alert("http://x", "") is False


def test_failures_never_raise(monkeypatch):
    """★通知のために開催日を落とすのは本末転倒。"""
    def _boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(notify.urllib.request, "urlopen", _boom)
    assert notify.heartbeat("http://x") is False
    assert notify.alert("http://x", "msg") is False


def test_alert_body_is_slack_compatible(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "_post",
                        lambda url, body, ct: seen.update(url=url, body=body, ct=ct) or True)
    notify.alert("http://hook", "こわれた")
    assert seen["ct"] == "application/json"
    assert b'"text"' in seen["body"] and "こわれた".encode() in seen["body"]


# --------------------------------------------------------------------------- #
# ★heartbeat は alert の有無に関わらず送る
# --------------------------------------------------------------------------- #
def _tick_with(monkeypatch, actions, **kw):
    monkeypatch.setattr(R, "collect", lambda *a, **k: _state())
    monkeypatch.setattr(R, "plan", lambda st, pol: actions)
    return R.tick(object(), _Az(), Config(), **kw)


def test_heartbeat_is_sent_even_when_there_is_an_alert(monkeypatch):
    """目的が違う。
       heartbeat が途切れる = 司令塔が動いていない
       alert が来る         = 司令塔は動いているが状況が悪い
    """
    beats = []
    _tick_with(monkeypatch, [Action(ALERT, "windows", "agent が死んでいます")],
               heartbeat=lambda d, failed=False: beats.append(d))
    assert len(beats) == 1
    assert "異常" in beats[0]


def test_heartbeat_is_sent_when_nothing_happens(monkeypatch):
    """★何もしない日こそ ping が要る。止まっているのか正常なのか区別できない。"""
    beats = []
    _tick_with(monkeypatch, [], heartbeat=lambda d, failed=False: beats.append(d))
    assert len(beats) == 1
    assert "行動: なし" in beats[0]


def test_heartbeat_is_sent_in_dry_run(monkeypatch):
    """timer が回っているかの確認にもなる。"""
    beats = []
    _tick_with(monkeypatch, [Action(WAIT, "", "まだ")], dry_run=True, heartbeat=lambda d, failed=False: beats.append(d))
    assert len(beats) == 1


def test_a_failing_heartbeat_does_not_break_the_tick(monkeypatch):
    def _boom(detail, failed=False):
        raise RuntimeError("ping 失敗")

    out = _tick_with(monkeypatch, [], heartbeat=_boom)
    assert out.actions == []        # 1周は完了している


def test_summary_tells_what_happened(monkeypatch):
    beats = []
    _tick_with(monkeypatch, [Action(WAIT, "", "x")], heartbeat=lambda d, failed=False: beats.append(d))
    assert "20261024" in beats[0] and "開催日" in beats[0]


# --------------------------------------------------------------------------- #
# ★異常は /fail で即座に鳴らす
# --------------------------------------------------------------------------- #
def test_failure_pings_the_fail_endpoint(monkeypatch):
    """★死活監視が「動いていない」しか拾えないと、
       『動いているが agent が上がらない』を取り逃す。
    """
    seen = {}
    monkeypatch.setattr(notify, "_post",
                        lambda url, body, ct: seen.update(url=url) or True)
    notify.heartbeat("https://hc-ping.com/abc", failed=True)
    assert seen["url"] == "https://hc-ping.com/abc/fail"


def test_success_pings_the_plain_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "_post",
                        lambda url, body, ct: seen.update(url=url) or True)
    notify.heartbeat("https://hc-ping.com/abc")
    assert seen["url"] == "https://hc-ping.com/abc"


def test_a_trailing_slash_does_not_break_the_fail_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(notify, "_post",
                        lambda url, body, ct: seen.update(url=url) or True)
    notify.heartbeat("https://hc-ping.com/abc/", failed=True)
    assert seen["url"] == "https://hc-ping.com/abc/fail"


def test_an_alert_makes_the_tick_ping_fail(monkeypatch):
    got = {}
    monkeypatch.setattr(R, "collect", lambda *a, **k: _state())
    monkeypatch.setattr(R, "plan", lambda st, pol: [Action(ALERT, "windows", "死んでいます")])
    R.tick(object(), _Az(), Config(),
           heartbeat=lambda d, failed=False: got.update(detail=d, failed=failed))
    assert got["failed"] is True


def test_a_normal_tick_pings_success(monkeypatch):
    got = {}
    monkeypatch.setattr(R, "collect", lambda *a, **k: _state())
    monkeypatch.setattr(R, "plan", lambda st, pol: [Action(WAIT, "", "まだ")])
    R.tick(object(), _Az(), Config(),
           heartbeat=lambda d, failed=False: got.update(failed=failed))
    assert got["failed"] is False
