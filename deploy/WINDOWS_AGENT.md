# Windows の agent を「ログイン無し」で起動させる

★**これが自動化の前提**。`az vm start` は Windows を起こすが、**誰もログインしない**。
agent がログイン契機で起動する設定のままだと、VM は立つのに agent は上がらず、
司令塔は「応答しません」と鳴らすだけになる。

## 先に知っておく落とし穴

- **JV-Link のサービスキーは HKCU(ユーザ単位)**。`SYSTEM` で走らせると見えない
- IPAT のレシピ(`C:\Users\<user>\ipat_recipe.json`)もプロファイル配下
- → **「そのユーザとして、ログオン無しで」** 走らせる必要がある
- そのユーザは **32ビット Python の venv** を使うこと(JV-Link は32ビット COM)

## 設定(タスクスケジューラ)

GUI なら「ユーザーがログオンしているかどうかにかかわらず実行する」を選ぶ。
コマンドなら:

```powershell
$User = "$env:COMPUTERNAME\kyosuke.tomoda"
$Dir  = "C:\Users\kyosuke.tomoda\Documents\hro\hro-operations"

$Action  = New-ScheduledTaskAction -Execute "cmd.exe" `
  -Argument "/c cd /d $Dir && poetry run hro-ops agent --server windows" `
  -WorkingDirectory $Dir
# ★起動直後はネットワークやサービスが揃っていない。1〜2分遅らせる
$Trigger = New-ScheduledTaskTrigger -AtStartup
$Trigger.Delay = "PT2M"
# ★落ちたら上げ直す。開催日に黙って止まるのが一番高い
$Settings = New-ScheduledTaskSettingsSet -RestartInterval (New-TimeSpan -Minutes 1) `
  -RestartCount 10 -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
  -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName "hro-agent-windows" -Action $Action `
  -Trigger $Trigger -Settings $Settings -User $User -RunLevel Highest -Password (Read-Host -AsSecureString)
```

`-Password` を渡すと「ログオン無しで実行」になる。パスワードは Windows が
LSA シークレットとして保持する(タスク定義には平文で残らない)。

★**パスワードを変更したら、このタスクは黙って失敗する**。変更したら登録し直すこと。

## ★必ず確かめること(推測で済ませない)

設定しただけでは分からない。**人が一切触らずに上がるか**を実際に試す。

```bash
# Linux 機(hro-calc-prod1)から、非開催日に
poetry run hro-orchestrator coldstart-test
```

停止 → 起動 → agent のハートビート待ち、を自動でやる。
**待っている間、絶対に RDP でログインしないこと**(ログイン契機で上がってしまい、
検証にならない)。開催日は拒否される(VM を止めるので、その日が終わるため)。

```
○ 人が触らずに agent が上がりました(187秒)
```

これが出るまで、司令塔の自動化(timer)を有効にしないこと。

## JV-Link が非対話セッションで動くか

これも `coldstart-test` の後に確かめる。agent が上がっても、JV-Link が
対話セッションを要求するなら発注まで到達しない。

```bash
# Linux 機から windows 宛にジョブを投げて、結果を見る
# (admin UI の「ジョブ」から netkeiba_check 相当の win-check でもよい)
```

Windows 側に RDP せず、**ジョブの結果だけで判断する**のが要点。
RDP で繋ぐと対話セッションができてしまい、本番と条件が変わる。
