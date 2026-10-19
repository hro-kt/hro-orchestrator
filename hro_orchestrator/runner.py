"""計画を実行する。**判断はしない**(すべて plan が決める)。

★ここに if を増やしたら負け。安全規則が2箇所に散ると、片方だけ直して事故る。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from . import db as dbmod
from .azure_vm import AzureVms
from .config import Config
from .plan import (ALERT, ENQUEUE, JST, START_VM, STOP_VM, WAIT,
                   Action, DayState, VmState, plan)

log = logging.getLogger(__name__)


@dataclass
class Outcome:
    actions: list[Action]
    done: list[str]
    alerts: list[str]
    dry_run: bool

    def ok(self) -> bool:
        return not self.alerts


def collect(conn, az: AzureVms, cfg: Config, *, now: datetime | None = None) -> DayState:
    """いまの状態を集める。★ここでは何も決めない。"""
    now = now or datetime.now(JST)
    date = now.strftime("%Y%m%d")
    first, last, n = dbmod.posts(conn, date)
    pol = cfg.policy
    try:
        running = az.is_running(pol.windows_vm)
        power = "running" if running else "stopped"
    except Exception as e:      # noqa: BLE001 - 読めないことは「止まっている」ではない
        log.error("VM の電源状態を読めません: %s", e)
        running, power = True, f"unknown({type(e).__name__})"
        # ★読めないときは「動いている」として扱う。止める判断に倒すと、
        #   読めないだけで走っているジョブごと落とす。
    return DayState(
        now=now, date=date, is_race_day=n > 0,
        first_post=first, last_post=last,
        vms={pol.windows_vm: VmState(pol.windows_vm, running, power)},
        agents=dbmod.live_agents(conn),
        jobs=dbmod.job_status(conn, date),
        busy=dbmod.busy_by_target(conn))


def execute(conn, az: AzureVms, cfg: Config, actions: list[Action], *,
            dry_run: bool = False, alert=None) -> Outcome:
    done: list[str] = []
    alerts: list[str] = []
    for a in actions:
        if a.kind == WAIT:
            log.info("待機: %s", a.reason)
            continue
        if a.kind == ALERT:
            log.error("★%s: %s", a.target or "-", a.reason)
            alerts.append(f"{a.target}: {a.reason}")
            if alert and not dry_run:
                alert(f"[hro] {a.target}: {a.reason}")
            continue
        if dry_run:
            log.info("(dry-run) %s", a)
            done.append(f"(dry-run) {a}")
            continue
        if a.kind == START_VM:
            az.start(a.target)
            log.warning("%s を起動しました(%s)", a.target, a.reason)
            done.append(f"start {a.target}")
        elif a.kind == STOP_VM:
            az.stop(a.target)
            log.warning("%s を停止しました(%s)", a.target, a.reason)
            done.append(f"stop {a.target}")
        elif a.kind == ENQUEUE:
            jid = dbmod.enqueue_race_day(conn, _date_of(actions, cfg),
                                         cfg.flow_args_json)
            log.warning("race_day job#%s を投入しました(%s)", jid, a.reason)
            done.append(f"enqueue race_day job#{jid}")
        else:
            log.error("未知の行動: %s", a)
            alerts.append(f"未知の行動: {a}")
    return Outcome(actions=actions, done=done, alerts=alerts, dry_run=dry_run)


def _date_of(actions: list[Action], cfg: Config) -> str:
    """投入する日付。collect と同じ基準(JST の当日)。"""
    return datetime.now(JST).strftime("%Y%m%d")


def tick(conn, az: AzureVms, cfg: Config, *, dry_run: bool = False,
         now: datetime | None = None, alert=None) -> Outcome:
    """1周。★冪等。何度呼んでも安全。"""
    st = collect(conn, az, cfg, now=now)
    return execute(conn, az, cfg, plan(st, cfg.policy), dry_run=dry_run, alert=alert)
