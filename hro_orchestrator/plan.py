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
ENQUEUE_JOB = "enqueue_job"     # target = ジョブ種別(朝の支度)
CANCEL_JOB = "cancel_job"       # target = ジョブ種別
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

    # --- 朝の支度 ---------------------------------------------------------
    # ★発走前に流しておく重い仕事。この順に**1本ずつ**(JV-Link は1台1プロセス)。
    #   空にすると朝の支度をしない(これまでの挙動)。
    morning_jobs: tuple[str, ...] = ("sync_all",)
    # 朝の支度がある日は、これだけ前に VM を起こす。sync-all は時間がかかる。
    morning_lead_hours: float = 6.0
    # これを過ぎたら**新しくは始めない**。始めると終わらない。
    morning_cutoff_hours: float = 3.0
    # これを過ぎてもまだ走っていたら**打ち切る**。発注を優先する。
    morning_last_call_hours: float = 2.5
    # JV-Link を占有する種別。これが走っている間は race_day を入れない。
    jvlink_kinds: tuple[str, ...] = ("sync_all", "fetch_ts_odds", "reparse")

    def __post_init__(self) -> None:
        # ★順序が狂うと「起こす前に締切」「打ち切る前に投入」になる。
        #   設定の矛盾は、走り出す前にここで落とす。
        if not self.morning_jobs:
            return
        order = [("morning_lead_hours", self.morning_lead_hours),
                 ("morning_cutoff_hours", self.morning_cutoff_hours),
                 ("morning_last_call_hours", self.morning_last_call_hours),
                 ("enqueue_lead_hours", self.enqueue_lead_hours)]
        for (an, av), (bn, bv) in zip(order, order[1:]):
            if av < bv:
                raise ValueError(
                    f"{an}({av}) は {bn}({bv}) 以上である必要があります。"
                    "起こす > 始めない > 打ち切る > race_day 投入 の順で早い")


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

    # ★朝の支度がある日は早く起こす。sync-all は時間がかかるので、
    #   第1発走の3時間前に起こしても間に合わない。
    lead = max(pol.start_lead_hours,
               pol.morning_lead_hours if pol.morning_jobs else 0.0)
    start_at = st.first_post - timedelta(hours=lead)
    enqueue_at = st.first_post - timedelta(hours=pol.enqueue_lead_hours)

    # ---- 起こす ------------------------------------------------------------
    if st.now < start_at:
        return [Action(WAIT, "", f"{start_at:%H:%M} に {pol.windows_vm} を起こします")]
    if win is not None and not win.running:
        return [Action(START_VM, pol.windows_vm,
                       f"第1発走 {st.first_post:%H:%M} の {lead:.0f} 時間前")]

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

    # ---- 朝の支度 ----------------------------------------------------------
    # ★race_day を入れる前だけ。入れた後は race_day 自身が JV-Link の段取りを持つ
    #   (常駐を止めてから sync_all → close_day)。二重に面倒を見ない。
    race_day = st.jobs.get("race_day")
    if race_day is None:
        m_acts, jv_busy = _plan_morning(st, pol)
        acts += m_acts
        if jv_busy:
            # ★JV-Link を掴んだままの仕事がある間に race_day を入れてはいけない。
            #   race_day は run_odds を**見送って**先へ進む(orchestrator._run_step)。
            #   見送られると速報オッズが一日来ず、1件も買えない。
            return acts

    # ---- 投入する ----------------------------------------------------------
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


def _plan_morning(st: DayState, pol: Policy) -> tuple[list[Action], bool]:
    """発走前に流しておく重い仕事。戻り値は (行動, JV-Link が塞がっているか)。

    ★1本ずつしか出さない。JV-Link は1台につき1プロセスで、重ねると COM が壊れる。
    ★締切を2段で持つ:
        morning_cutoff     これ以降は**新しくは始めない**(始めても終わらない)
        morning_last_call  これ以降も走っていたら**打ち切る**
      打ち切るのは乱暴に見えるが、残すほうが高くつく。sync_all が走ったまま
      race_day が始まると run_odds が見送られ、**その日は1件も買えない**。
      同期は SYNC_SMART が frontier から取り直すので、打ち切っても失わない。
    """
    assert st.first_post is not None
    acts: list[Action] = []
    busy = [k for k in pol.jvlink_kinds if st.jobs.get(k) in _LIVE]
    if busy:
        last_call = st.first_post - timedelta(hours=pol.morning_last_call_hours)
        if st.now >= last_call:
            acts.append(Action(CANCEL_JOB, busy[0],
                               f"第1発走 {st.first_post:%H:%M} が近いのに {busy[0]} が"
                               "走っています。このままだと run_odds が見送られて"
                               "一日買えません。同期を打ち切って発注を優先します"))
            acts.append(Action(ALERT, busy[0],
                               f"{busy[0]} を打ち切りました。取り込みは次の機会に"
                               "持ち越します(SYNC_SMART が続きから取ります)"))
        else:
            acts.append(Action(WAIT, busy[0],
                               f"{busy[0]} が走っています。終わるまで race_day は"
                               f"入れません(打ち切りは {last_call:%H:%M})"))
        return acts, True

    if not pol.morning_jobs:
        return acts, False

    cutoff = st.first_post - timedelta(hours=pol.morning_cutoff_hours)
    for kind in pol.morning_jobs:
        s = st.jobs.get(kind)
        if s == "done":
            continue
        if s == "failed":
            # ★鳴らすだけ。黙って入れ直さないし、発注は止めない。
            acts.append(Action(ALERT, kind,
                               f"朝の {kind} が failed です。取り込みは欠けますが、"
                               "発注はこのまま進めます"))
            continue
        if st.now >= cutoff:
            acts.append(Action(ALERT, kind,
                               f"朝の {kind} を今日は流しません({cutoff:%H:%M} を"
                               "過ぎました)。始めると第1発走に間に合いません"))
            continue
        acts.append(Action(ENQUEUE_JOB, kind,
                           f"第1発走 {st.first_post:%H:%M} の前に流します"))
        return acts, True        # ★1本ずつ。次の周回で次の1本
    return acts, False


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
