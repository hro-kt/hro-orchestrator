"""Azure VM の電源。az CLI 経由。

★SDK ではなく az CLI を使う理由:
  - マネージドID の取り回しが `az login --identity` だけで済む
  - 運用者が同じコマンドを手で打って確認できる(障害時に大事)
  - 依存が増えない
  代わりに az の存在と権限が前提になるので、起動時に確かめる。

★dry_run では**プロセスを起動しない**。「実行したつもり」を型で防ぐ。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

RUNNING = "VM running"
DEALLOCATED = "VM deallocated"


class AzureError(RuntimeError):
    pass


@dataclass
class AzureVms:
    resource_group: str
    dry_run: bool = False
    timeout_sec: float = 300.0
    # 差し替え用(テスト)。None なら subprocess を使う
    runner = None

    def _az(self, args: list[str]) -> str:
        if self.runner is not None:
            return self.runner(args)
        if shutil.which("az") is None:
            raise AzureError(
                "az コマンドがありません。Azure CLI を入れ、"
                "`az login --identity` でマネージドID にログインしてください")
        p = subprocess.run(["az", *args], capture_output=True, text=True,
                           timeout=self.timeout_sec)
        if p.returncode != 0:
            raise AzureError(f"az {' '.join(args)} が失敗: "
                             f"{(p.stderr or p.stdout).strip()[:300]}")
        return p.stdout

    # ---- 読む(dry_run でも実行してよい) ------------------------------------
    def power_state(self, name: str) -> str:
        """'VM running' / 'VM deallocated' 等。読めなければ例外。"""
        out = self._az(["vm", "get-instance-view", "-g", self.resource_group,
                        "-n", name, "--query",
                        "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus",
                        "-o", "json"])
        try:
            v = json.loads(out or "[]")
        except json.JSONDecodeError as e:
            raise AzureError(f"電源状態を読めません({name}): {out[:200]}") from e
        if not v:
            raise AzureError(f"電源状態が空です({name})。VM 名と資源グループを確認してください")
        return str(v[0])

    def is_running(self, name: str) -> bool:
        return self.power_state(name) == RUNNING

    # ---- 書く(dry_run では何もしない) --------------------------------------
    def start(self, name: str) -> bool:
        if self.dry_run:
            return False
        self._az(["vm", "start", "-g", self.resource_group, "-n", name])
        return True

    def stop(self, name: str) -> bool:
        """★deallocate する。`vm stop` だけでは課金が止まらない。"""
        if self.dry_run:
            return False
        self._az(["vm", "deallocate", "-g", self.resource_group, "-n", name])
        return True

    def preflight(self) -> list[str]:
        """使える状態か。問題の一覧(空なら OK)。"""
        probs: list[str] = []
        if shutil.which("az") is None and self.runner is None:
            probs.append("az コマンドがありません")
            return probs
        try:
            self._az(["account", "show", "-o", "json"])
        except Exception as e:      # noqa: BLE001
            probs.append(f"Azure にログインできていません({str(e)[:120]})。"
                         "`az login --identity` を実行してください")
        return probs
