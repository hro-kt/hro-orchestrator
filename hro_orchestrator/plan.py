"""その日に何をすべきかを決める。**純粋関数**。

★DB も Azure も触らない。「いまの状態」を受け取って「次にやるべき行動の列」を返す。
  この5日間に潰した障害は、どれも**副作用の中に判断が埋まっていて試せない**形だった
  (締切判定・再投入・preflight・二重起動)。同じ作りにしない。

安全規則はすべてここに書く。runner はここが返した行動を実行するだけ。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

# 行動の種類
START_VM = "start_vm"
STOP_VM = "stop_vm"
ENQUEUE = "enqueue_race_day"
ALERT = "alert"
WAIT = "wait"


@dataclass(frozen=True)
class Action:
    kind: str
    target: str = ""           # VM 名 / ジョブ種別
    reason: str = ""           # なぜそうするか(ログと通知に出す)

    def __str__(self) -> str:
        t = f" {self.target}" if self.target else ""
        return f"{self.kind}{t}: {self.reason}"


@dataclass
class VmState:
    name: str
    running: bool
    # ★「電源が入っている」と「使える」は別。az から見た状態をそのまま持つ。
    power: str = ""
    # ★起動中/停止中。ここを「止まっている」と読むと、起動中に起動を打ち直す。
    transitioning: bool = False


@dataclass
class DayState:
    """いまの状態。runner が DB と Azure から集めて渡す。"""
    now: datetime
    date: str                       # YYYYMMDD(JST)
    is_race_day: bool               # nl_ra に当日のレースがあるか
    first_post: datetime | None     # 当日の最初の発走(JST)
    last_post: datetime | None      # 当日の最後の発走(JST)
    vms: dict[str, VmState] = field(default_factory=dict)
    # target(windows/vm) -> agent が生きているか
    agents: dict[str, bool] = field(default_factory=dict)
    # kind -> その日の最新ジョブの状態("queued"/"running"/"done"/"failed"/None)
    jobs: dict[str, str | None] = field(default_factory=dict)
    # target -> その機で queued|running のジョブ数
    busy: dict[str, int] = field(default_factory=dict)
    # ★翌日のレース数(nl_ra)。0 のまま開催日を迎えると、司令塔は
    #   「今日は非開催日」と判断して VM を起こさない。前日に気づきたい。
    tomorrow_races: int = 0


@dataclass(frozen=True)
class Policy:
    """いつ何をするか。すべて JST。"""
    # 対象 VM と、その VM が担う target
    windows_vm: str = "hro-sync-prod2"
    windows_target: str = "windows"
    # 第1発走のこれだけ前に VM を起こす。
    # ★人が手で起こせる余裕を残す。起動に失敗した日は、気づくのが遅いと丸ごと失う。
    start_lead_hours: float = 3.0
    # 起こしてからこれだけ経っても agent が応答しなければ鳴らす
    agent_wait_minutes: float = 20.0
    # 最終発走のこれだけ後まで止めない(締めと封印の猶予)
    stop_after_minutes: float = 30.0
    # race_day を投入するのは第1発走のこれだけ前から
    enqueue_lead_hours: float = 2.0
    # 翌日が土日なのに nl_ra に1レースも無ければ鳴らす(RACE データが古い)
    warn_stale_race_data: bool = True


_LIVE = ("queued", "running")
_FINISH_KINDS = ("close_day", "tax_seal")


def plan(st: DayState, pol: Policy = Policy()) -> list[Action]:
    """次にやるべき行動。空なら何もしない。"""
    acts: list[Action] = []
    win = st.vms.get(pol.windows_vm)

    # ★遷移中は何もしない。起動中に起動を打ち直すと、ログが汚れて
    #   「何が起きているか分からない」状態になる。次の周回で落ち着く。
    if win is not None and win.transitioning:
        return [Action(WAIT, pol.windows_vm, f"遷移中です({win.power})")]

    if not st.is_race_day:
        return _plan_idle(st, pol, win)

    if st.first_post is None:
        return [Action(ALERT, pol.windows_vm,
                       "開催日なのに発走時刻が読めません(nl_ra の hasso_time)。"
                       "RACE の取り込みを確認してください")]

    start_at = st.first_post - timedelta(hours=pol.start_lead_hours)
    enqueue_at = st.first_post - timedelta(hours=pol.enqueue_lead_hours)

    # ---- 起こす ------------------------------------------------------------
    if st.now < start_at:
        return [Action(WAIT, "", f"{start_at:%H:%M} に {pol.windows_vm} を起こします")]
    if win is not None and not win.running:
        return [Action(START_VM, pol.windows_vm,
                       f"第1発走 {st.first_post:%H:%M} の {pol.start_lead_hours:.0f} 時間前")]

    # ---- agent を待つ ------------------------------------------------------
    # ★「VM が起動した」≠「agent が動いている」。ここを飛ばすと、
    #   誰も拾わないジョブを投入して「正常に見えるが0件」になる。
    if not st.agents.get(pol.windows_target):
        if st.now >= start_at + timedelta(minutes=pol.agent_wait_minutes):
            return [Action(ALERT, pol.windows_target,
                           f"{pol.windows_vm} を起こしてから "
                           f"{pol.agent_wait_minutes:.0f} 分経っても agent が応答しません。"
                           "手で確認してください(第1発走まで時間があります)")]
        return [Action(WAIT, pol.windows_target, "agent の応答を待っています")]

    # ---- 投入する ----------------------------------------------------------
    race_day = st.jobs.get("race_day")
    if race_day in _LIVE:
        acts.append(Action(WAIT, "race_day", "当日のオーケストレータが動いています"))
    elif race_day == "failed":
        # ★落ちたものを黙って入れ直さない。同じ理由で落ちるなら何度やっても同じ。
        acts.append(Action(ALERT, "race_day",
                           "当日のオーケストレータが failed で終わっています。"
                           "ログを見てから手で入れ直してください"))
    elif race_day is None:
        if st.now >= enqueue_at:
            acts.append(Action(ENQUEUE, "race_day",
                               f"第1発走 {st.first_post:%H:%M} の "
                               f"{pol.enqueue_lead_hours:.0f} 時間前"))
        else:
            acts.append(Action(WAIT, "race_day", f"{enqueue_at:%H:%M} に投入します"))

    # ---- 止める ------------------------------------------------------------
    acts += _plan_stop(st, pol, win)
    return acts


def _stale_race_data(st: DayState, pol: Policy) -> list[Action]:
    """翌日が土日なのに nl_ra にレースが無い = RACE データが古い。

    ★司令塔は nl_ra を見て開催日を判断する。古いままだと
      「今日は非開催日」として**VM を起こさず、一日が静かに終わる**。
      しかも fetch_race は JV-Link(Windows VM)が要るので、気づくのが
      当日では遅い。前日のうちに鳴らす。
    ★平日は判定しない。JRA の出走表は週後半に配信されるので、
      月〜水に「将来のレースが無い」のは正常。
    """
    if not pol.warn_stale_race_data:
        return []
    tomorrow = st.now + timedelta(days=1)
    if tomorrow.weekday() not in (5, 6):        # 土=5 日=6
        return []
    if st.tomorrow_races > 0:
        return []
    return [Action(ALERT, "nl_ra",
                   f"明日({tomorrow:%m/%d})は土日ですが nl_ra にレースがありません。"
                   "RACE データが古いと、司令塔は明日を非開催日と判断して "
                   "VM を起こしません。今日のうちに fetch_race を流してください"
                   "(JV-Link が要るので Windows VM が動いている必要があります)")]


def _plan_idle(st: DayState, pol: Policy, win: VmState | None) -> list[Action]:
    """非開催日。基本は止まっているべきだが、仕事があるなら起こす。"""
    acts = _stale_race_data(st, pol)
    busy = st.busy.get(pol.windows_target, 0)
    if win is None:
        return acts
    if not win.running:
        # ★止めるだけでは片手落ち。VM が止まっている間に admin から
        #   Windows 宛のジョブ(sync_all / settle / close_day 等)を投入すると、
        #   拾う agent が居らず**永久に queued のまま**になる。
        #   「誰も気づかないまま何も起きない」は、この系で一番高くつく壊れ方。
        if busy:
            acts.append(Action(START_VM, pol.windows_vm,
                               f"{pol.windows_target} 宛のジョブが {busy} 件待っています"))
        return acts
    if busy:
        # ★走っているものがあるなら止めない。開催日でなくても、
        #   後日の決済や同期が回っていることがある。
        acts.append(Action(WAIT, pol.windows_vm,
                           f"非開催日ですが {busy} 件のジョブが待機/実行中です。止めません"))
        return acts
    acts.append(Action(STOP_VM, pol.windows_vm, "非開催日で、待っているジョブもありません"))
    return acts


def _plan_stop(st: DayState, pol: Policy, win: VmState | None) -> list[Action]:
    if win is None or not win.running or st.last_post is None:
        return []
    if st.now < st.last_post + timedelta(minutes=pol.stop_after_minutes):
        return []
    # ★締めと封印が終わるまで止めない。close_day は IPAT の投票履歴を読むが、
    #   IPAT で遡れるのは当日と前日だけ。ここで電源を切ると当日の確定値を失う。
    for kind in _FINISH_KINDS:
        if st.jobs.get(kind) != "done":
            return [Action(WAIT, pol.windows_vm,
                           f"{kind} が終わっていません({st.jobs.get(kind) or '未投入'})。"
                           "締めが済むまで止めません")]
    busy = st.busy.get(pol.windows_target, 0)
    if busy:
        return [Action(WAIT, pol.windows_vm,
                       f"{busy} 件のジョブが走っています。止めません")]
    return [Action(STOP_VM, pol.windows_vm, "最終発走から時間が経ち、締めも封印も完了")]
