"""設定。環境変数(.env 含む) > 既定値。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .plan import Policy


@dataclass
class PostgresConfig:
    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "hro"
    user: str = "postgres"
    password: str = ""
    sslmode: str = ""


@dataclass
class Config:
    postgres: PostgresConfig = field(default_factory=PostgresConfig)
    policy: Policy = field(default_factory=Policy)
    resource_group: str = "prod1"
    # race_day に渡す引数(JSON 文字列)。UI から渡しているものと同じ形。
    flow_args_json: str = ""
    # 通知先(未設定ならログだけ)
    alert_webhook: str = ""


def _f(key: str, default: float) -> float:
    v = os.environ.get(key)
    return float(v) if v else default


def load_config() -> Config:
    load_dotenv()
    e = os.environ.get
    pg = PostgresConfig(
        host=e("POSTGRES_HOST", "127.0.0.1"),
        port=int(e("POSTGRES_PORT", "5432")),
        dbname=e("POSTGRES_DATABASE", "hro"),
        user=e("POSTGRES_USER", "postgres"),
        password=e("POSTGRES_PASSWORD", ""),
        sslmode=e("POSTGRES_SSLMODE", ""),
    )
    pol = Policy(
        windows_vm=e("ORC_WINDOWS_VM", "hro-sync-prod2"),
        windows_target=e("ORC_WINDOWS_TARGET", "windows"),
        start_lead_hours=_f("ORC_START_LEAD_HOURS", 3.0),
        agent_wait_minutes=_f("ORC_AGENT_WAIT_MIN", 20.0),
        stop_after_minutes=_f("ORC_STOP_AFTER_MIN", 30.0),
        enqueue_lead_hours=_f("ORC_ENQUEUE_LEAD_HOURS", 2.0),
    )
    return Config(postgres=pg, policy=pol,
                  resource_group=e("ORC_RESOURCE_GROUP", "prod1"),
                  flow_args_json=e("ORC_FLOW_ARGS", ""),
                  alert_webhook=e("ORC_ALERT_WEBHOOK", ""))
