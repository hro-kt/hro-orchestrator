"""CLI。"""

from __future__ import annotations

import logging

import click

from .azure_vm import AzureVms
from .config import load_config


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
def status_cmd() -> None:
    """VM / agent / ジョブの現状を出す(何も変えない)。"""
    from . import db as dbmod
    from .runner import collect

    cfg, az = _setup(dry_run=True)
    conn = dbmod.connect(cfg)
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
def plan_cmd() -> None:
    """いま何をするつもりかだけ出す(副作用なし)。"""
    from . import db as dbmod
    from .plan import plan as make_plan
    from .runner import collect

    cfg, az = _setup(dry_run=True)
    conn = dbmod.connect(cfg)
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
    conn = dbmod.connect(cfg)
    out = tick(conn, az, cfg, dry_run=dry_run)
    for a in out.actions:
        click.echo(f"  {a}")
    for d in out.done:
        click.echo(f"  → {d}")
    raise SystemExit(1 if out.alerts else 0)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
