"""CLI。"""

from __future__ import annotations

import logging

import click

from .azure_vm import AzureVms
from .config import load_config


def _guard_db(fn):
    """DB 不達をトレースバックではなく1行で見せる。"""
    import functools

    @functools.wraps(fn)
    def _wrap(*a, **k):
        from .db import DbUnavailable
        try:
            return fn(*a, **k)
        except DbUnavailable as e:
            click.echo(f"✗ {e}")
            raise SystemExit(2) from None

    return _wrap


def _setup(dry_run: bool):
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config()
    az = AzureVms(resource_group=cfg.resource_group, dry_run=dry_run)
    return cfg, az


@click.group()
def cli() -> None:
    """開催日の司令塔。Azure の電源と、当日ジョブの引き金。"""


@cli.command("status")
@_guard_db
def status_cmd() -> None:
    """VM / agent / ジョブの現状を出す(何も変えない)。"""
    from . import db as dbmod
    from .runner import collect

    cfg, az = _setup(dry_run=True)
    conn = dbmod.connect_or_explain(cfg)
    st = collect(conn, az, cfg)
    click.echo(f"=== {st.date} ===")
    click.echo(f"  開催日      : {'はい' if st.is_race_day else 'いいえ'}")
    if st.first_post:
        click.echo(f"  第1発走     : {st.first_post:%H:%M}")
        click.echo(f"  最終発走    : {st.last_post:%H:%M}")
    for name, v in st.vms.items():
        click.echo(f"  VM {name:<18}: {'起動' if v.running else '停止'}  ({v.power})")
    click.echo(f"  agent       : " + ", ".join(
        f"{k}={'生' if v else '×'}" for k, v in sorted(st.agents.items())) or "(なし)")
    if st.jobs:
        click.echo("  当日のジョブ:")
        for k, v in sorted(st.jobs.items()):
            click.echo(f"      {k:<16} {v}")
    if st.busy:
        click.echo("  走っているジョブ: " + ", ".join(
            f"{k}={v}" for k, v in sorted(st.busy.items())))


@cli.command("plan")
@_guard_db
def plan_cmd() -> None:
    """いま何をするつもりかだけ出す(副作用なし)。"""
    from . import db as dbmod
    from .plan import plan as make_plan
    from .runner import collect

    cfg, az = _setup(dry_run=True)
    conn = dbmod.connect_or_explain(cfg)
    acts = make_plan(collect(conn, az, cfg), cfg.policy)
    if not acts:
        click.echo("することはありません")
        return
    for a in acts:
        click.echo(f"  {a}")


@cli.command("run")
@click.option("--dry-run", is_flag=True, help="行動を出すが実行しない")
@click.option("--check-azure/--no-check-azure", default=True,
              help="az が使えるかを先に確かめる")
@_guard_db
def run_cmd(dry_run, check_azure) -> None:
    """1周ぶん実行する。★冪等なので、timer から何度呼んでも安全。"""
    from . import db as dbmod
    from .runner import tick

    cfg, az = _setup(dry_run=dry_run)
    if check_azure:
        probs = az.preflight()
        if probs:
            for p in probs:
                click.echo(f"✗ {p}")
            raise SystemExit(2)
    from .notify import alert as send_alert
    from .notify import heartbeat as send_heartbeat

    conn = dbmod.connect_or_explain(cfg)
    out = tick(conn, az, cfg, dry_run=dry_run,
               alert=(lambda m: send_alert(cfg.alert_webhook, m))
               if cfg.alert_webhook else None,
               heartbeat=(lambda d: send_heartbeat(cfg.heartbeat_url, detail=d))
               if cfg.heartbeat_url else None)
    if not cfg.heartbeat_url:
        click.echo("  ※ ORC_HEARTBEAT_URL が未設定です。"
                   "**司令塔自身が落ちたことに誰も気づけません**")
    for a in out.actions:
        click.echo(f"  {a}")
    for d in out.done:
        click.echo(f"  → {d}")
    raise SystemExit(1 if out.alerts else 0)


@cli.command("coldstart-test")
@click.option("--timeout-min", type=float, default=15.0,
              help="agent の応答を待つ上限(分)")
@click.option("--skip-stop", is_flag=True,
              help="既に停止しているので、止めずに起こすところから試す")
@click.option("--force", is_flag=True,
              help="開催日でも実行する(★その日が終わります)")
@_guard_db
def coldstart_cmd(timeout_min, skip_stop, force) -> None:
    """★人が一切触らずに agent が上がるかを確かめる。

    `az vm start` は Windows を起こすが**誰もログインしない**。agent が
    ログイン契機で起動する設定のままだと、VM は立つのに agent は上がらず、
    司令塔は「応答しません」と鳴らすだけになる。自動化の前提がそこにある。

    ★VM を止めるので、開催日には走らせないこと。
    """
    from . import db as dbmod
    from .coldstart import refuse_reason, run as cold_run
    from .runner import collect

    cfg, az = _setup(dry_run=False)
    conn = dbmod.connect_or_explain(cfg)
    st = collect(conn, az, cfg)
    pol = cfg.policy
    why = refuse_reason(is_race_day=st.is_race_day,
                        busy=st.busy.get(pol.windows_target, 0))
    if why and not force:
        click.echo(f"✗ {why}")
        click.echo("  (どうしても試すなら --force。★その日が終わります)")
        raise SystemExit(2)

    click.echo(f"=== 冷間起動の検証: {pol.windows_vm} ===")
    res = cold_run(vm=pol.windows_vm, target=pol.windows_target,
                   stop_vm=az.stop, start_vm=az.start,
                   wait_stopped=lambda v: az.wait_for_power(v, "VM deallocated"),
                   agent_alive=lambda t: dbmod.live_agents(conn).get(t, False),
                   timeout_sec=timeout_min * 60.0, skip_stop=skip_stop,
                   log=click.echo)
    click.echo("")
    for line in res.lines():
        click.echo(line)
    raise SystemExit(0 if res.ok else 1)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
