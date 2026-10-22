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

    def verdict(self) -> str:
        """結論の1行。★手順は実行中に出しているので、ここでは繰り返さない。"""
        if self.ok:
            return f"○ 人が触らずに agent が上がりました({self.elapsed_sec:.0f}秒)"
        return f"✗ {self.problem}"

    def lines(self) -> list[str]:
        return list(self.steps) + [self.verdict()]


def refuse_reason(*, is_race_day: bool, busy: int) -> str | None:
    """走らせてはいけない状況。

    ★VM を止めるので、開催日にやると**その日が終わる**。
    """
    if is_race_day:
        return "今日は開催日です。VM を止めるので実行しません"
    if busy:
        return f"その機で {busy} 件のジョブが走っています。実行しません"
    return None


def run(*, vm: str, target: str, stop_vm, start_vm, agent_seen_at, ref_now,
        timeout_sec: float = 900.0, poll_sec: float = 15.0,
        skip_stop: bool = False, sleep=time.sleep, wait_stopped=None,
        now=lambda: datetime.now().timestamp(), log=print) -> ColdStartResult:
    """停止 → 起動 → **起動後の**ハートビートを待つ。

    Args:
        agent_seen_at: target → 最後の心拍時刻(無ければ None)
        ref_now: DB 側の現在時刻を返す。★こちらの時計と比べない

    ★「生きているか」で見てはいけない。VM を落とした直後は心拍がまだ新しく、
      死んだ agent を生きていると誤判定する(2026-10-23 に0秒で合格した)。
      **起動を投げた時刻より後の心拍**でなければ合格にしない。
    """
    steps: list[str] = []

    def _say(msg: str) -> None:
        steps.append(msg)
        log("  " + msg)

    if not skip_stop:
        _say(f"{vm} を停止します(deallocate)")
        stop_vm(vm)
        # ★停止の**完了**を待ってから起こす。遷移中に起動を投げると弾かれる。
        #   停止/起動は --no-wait で投げるので、待つのはこちらの役目。
        if wait_stopped is not None and not wait_stopped(vm):
            return ColdStartResult(
                ok=False, steps=steps,
                problem=f"{vm} が停止しきりません。Azure ポータルで状態を確認してください")
        _say(f"{vm} は停止しました")
    # ★基準は DB 側の時刻。これより後の心拍だけを「起動後の心拍」と認める。
    mark = ref_now()
    _say(f"{vm} を起動します(基準時刻 {mark})")
    start_vm(vm)
    _say(f"agent({target})の**新しい**ハートビートを待ちます "
         f"…最大 {timeout_sec / 60:.0f} 分。**この間ログインしないでください**")

    t0 = now()
    while True:
        seen = agent_seen_at(target)
        if seen is not None and seen > mark:
            return ColdStartResult(ok=True, elapsed_sec=now() - t0, steps=steps)
        if now() - t0 >= timeout_sec:
            return ColdStartResult(
                ok=False, elapsed_sec=now() - t0, steps=steps,
                problem=(
                    f"{timeout_sec / 60:.0f} 分待っても agent({target})の"
                    "新しいハートビートがありません。\n"
                    "      ★タスクの設定を『ログオンしていなくても実行』に変えないこと。\n"
                    "        それは Session 0(非対話)で走るので **JV-Link が動かない**\n"
                    "        (対話的デスクトップを要求する。2026-09 確認)。\n"
                    "      正解は**自動ログオン**。OS 起動時に自動でログオンさせれば\n"
                    "      対話セッションができ、いまのタスクがそのまま発火します:\n"
                    "        ・Sysinternals Autologon で設定(LSA シークレットに暗号化保存)\n"
                    "        ・ログオンするユーザは、JV-Link のサービスキー(HKCU)と\n"
                    "          レシピ/state を持つ**いま運用しているユーザ**と同じにする\n"
                    "        ・画面ロック/スクリーンセーバーを無効にする\n"
                    "        ・RDP で入ったら『サインアウト』せず『切断』で抜ける\n"
                    "      手順: deploy/WINDOWS_AGENT.md"))
        sleep(poll_sec)
