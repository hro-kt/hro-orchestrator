"""冷間起動の検証。

★`az vm start` は Windows を起こすが、**誰もログインしない**。
  agent がログイン契機で起動する設定なら、VM は立つのに agent は上がらない。
  司令塔は「agent が応答しません」と鳴らすだけで、自動化として成立しない。

  ここで確かめるのは1点だけ:
    **人が一切触らずに、停止状態から agent のハートビートが出るか。**

★開催日には絶対に走らせない。VM を止めるので、その日が終わる。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ColdStartResult:
    ok: bool
    elapsed_sec: float = 0.0
    steps: list[str] = field(default_factory=list)
    problem: str = ""

    def lines(self) -> list[str]:
        out = list(self.steps)
        if self.ok:
            out.append(f"○ 人が触らずに agent が上がりました({self.elapsed_sec:.0f}秒)")
        else:
            out.append(f"✗ {self.problem}")
        return out


def refuse_reason(*, is_race_day: bool, busy: int) -> str | None:
    """走らせてはいけない状況。

    ★VM を止めるので、開催日にやると**その日が終わる**。
    """
    if is_race_day:
        return "今日は開催日です。VM を止めるので実行しません"
    if busy:
        return f"その機で {busy} 件のジョブが走っています。実行しません"
    return None


def run(*, vm: str, target: str, stop_vm, start_vm, agent_alive,
        timeout_sec: float = 900.0, poll_sec: float = 15.0,
        skip_stop: bool = False, sleep=time.sleep,
        now=lambda: datetime.now().timestamp(), log=print) -> ColdStartResult:
    """停止 → 起動 → agent のハートビートを待つ。

    引数はすべて関数で受ける(テストできるように)。
    """
    steps: list[str] = []

    def _say(msg: str) -> None:
        steps.append(msg)
        log("  " + msg)

    if not skip_stop:
        _say(f"{vm} を停止します(deallocate)")
        stop_vm(vm)
        # ★止めた直後に起こすと、Azure 側が遷移中で弾くことがある
        sleep(min(30.0, poll_sec * 2))
    _say(f"{vm} を起動します")
    start_vm(vm)
    _say(f"agent({target})のハートビートを待ちます "
         f"…最大 {timeout_sec / 60:.0f} 分。**この間ログインしないでください**")

    t0 = now()
    while True:
        if agent_alive(target):
            return ColdStartResult(ok=True, elapsed_sec=now() - t0, steps=steps)
        if now() - t0 >= timeout_sec:
            return ColdStartResult(
                ok=False, elapsed_sec=now() - t0, steps=steps,
                problem=(
                    f"{timeout_sec / 60:.0f} 分待っても agent({target})が応答しません。"
                    "ログイン契機で起動する設定のままだと、自動では上がりません。\n"
                    "      タスクスケジューラで次を満たしているか確認してください:\n"
                    "        ・実行ユーザ = JV-Link とレシピを持つユーザ\n"
                    "          (サービスキーは HKCU、レシピはプロファイル配下)\n"
                    "        ・「ユーザーがログオンしているかどうかにかかわらず実行する」\n"
                    "        ・トリガーは「スタートアップ時」(遅延を1〜2分)\n"
                    "        ・「タスクが失敗した場合の再起動」を1分間隔で数回"))
        sleep(poll_sec)
