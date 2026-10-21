# Windows の agent を「人が触らずに」上げる

★**これが自動化の前提**。`az vm start` は Windows を起こすが、**誰もログオンしない**。
agent がログオン契機で起動する設定なら、VM は立つのに agent は上がらない。

## ★先に: 「ログオンしていなくても実行」は**使えない**

```
Windows 側 agent はタスクスケジューラで ログオン時起動・対話セッション必須
(JV-Link が対話的デスクトップを要求するため service 不可)   — 2026-09 確認
```

タスクスケジューラの「ユーザーがログオンしているかどうかにかかわらず実行する」は
**Session 0(非対話)** で走る。**JV-Link はそこでは動かない**。
同じ理由で Windows サービス化も不可。

→ **タスクの設定を変えるのではなく、ログオンを自動化する。**

## 解: 自動ログオン(AutoAdminLogon)

OS 起動時に自動でログオンさせれば**対話セッションができ、いまのタスクがそのまま
発火する**。agent の起動方法は一切変えない(= 壊すリスクが最小)。

副次的な効果もある: Windows Update の自動再起動でランナーが消えて
**誰も気づかないまま5レースを失った**(2026-10-04)。自動ログオンなら再起動後に
自力で戻る。

### 手順

**1) Sysinternals Autologon を入れる**

```powershell
# 管理者 PowerShell で
Invoke-WebRequest https://download.sysinternals.com/files/AutoLogon.zip -OutFile $env:TEMP\al.zip
Expand-Archive $env:TEMP\al.zip -DestinationPath $env:TEMP\al -Force
```

**2) 設定する**

```powershell
# ★ドメイン参加していないなら、ドメインは「コンピュータ名」
& "$env:TEMP\al\Autologon64.exe" kyosuke.tomoda $env:COMPUTERNAME '<パスワード>'
```

初回は EULA の同意ダイアログが出る(`/accepteula` を付けると省ける)。
引数なしで起動すれば GUI でも入力できる。

★**レジストリを直接書く方法(`DefaultPassword`)は使わない。** 平文で残る。
  Autologon は **LSA シークレット**として暗号化保存する。

**3) 画面ロックを無効にする**

ロックされると対話セッションが制限され、JV-Link が失敗しうる。

```powershell
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
# スクリーンセーバーと、無操作でのロックを止める
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
  /v InactivityTimeoutSecs /t REG_DWORD /d 0 /f
```

**4) ★agent が2本立たないようにする**

自動ログオンを入れると、「ログオン時」のタスクが **2回**発火する:
起動時の自動ログオンと、あなたの RDP ログオン(同じユーザ、別セッション)。
agent が2本になると同じ待ち行列からジョブを奪い合い、**同じレースを2回買う**。

```powershell
# タスクを「既に実行中なら新しいインスタンスを開始しない」に
$t = Get-ScheduledTask -TaskName "<タスク名>"
$t.Settings.MultipleInstances = 'IgnoreNew'
Set-ScheduledTask -TaskName "<タスク名>" -Settings $t.Settings
```

ランチャ側(`hro-operations/scripts/run-agent-windows.ps1`)にも、既に agent が
走っていれば起動しない防御を入れてある。**両方効かせること。**

### 運用上の注意

- **RDP を切るときは「サインアウト」ではなく「切断」。** サインアウトすると
  セッションが消えて agent が落ちる
- Windows Server なので、RDP は**別セッション**になり、自動ログオンの
  コンソールセッション(と agent)はそのまま残る
- **NSG で RDP を自IPに限定**しておく(常に誰かがログオン状態になるため)
- **パスワードを変更したら Autologon を登録し直す**。しないと起動時に
  ログオンできず、agent も上がらない
- `HRO_HOME` の既定は `C:\hro` だが、この機の実体は
  `C:\Users\kyosuke.tomoda\Documents\hro`。ランチャの値を確認すること

### JV-Link のサービスキーとプロファイル

- サービスキーは **HKCU(ユーザ単位)**
- IPAT のレシピ / netkeiba の state も **プロファイル配下**
→ 自動ログオンするユーザは、**いま手で運用しているユーザと同じ**であること。

## ★必ず確かめる(推測で済ませない)

設定しただけでは分からない。**人が一切触らずに上がるか**を実際に試す。

### 実行前に満たしておくこと

| | 理由 |
|---|---|
| **非開催日である** | VM を止めるので、開催日にやるとその日が終わる(コマンドが拒否する) |
| **windows 宛のジョブが0件** | 走っているものを殺さない(コマンドが拒否する) |
| **自動ログオンを設定済み** | 未設定なら必ず失敗する。15分待つだけ無駄になる |
| **誰も RDP していない** | 既存セッションがあると、何が効いたのか分からなくなる |
| **待っている間も RDP しない** | ログオン契機で上がってしまい、**検証にならない** |
| Linux 機で `az login --identity` 済み | 電源を操作できない |

**VM が起動中でも停止中でも構わない。** コマンドが停止から始める。
既に停止しているなら `--skip-stop` で起こすところからにできる(数十秒早い)。

```bash
# Linux 機(hro-calc-prod1)から、非開催日に
poetry run hro-orchestrator coldstart-test            # 起動中/停止中どちらでも可
poetry run hro-orchestrator coldstart-test --skip-stop  # 既に停止しているとき
```

停止 → 起動 → agent のハートビート待ち、を自動でやる。
**待っている間、絶対に RDP で接続しないこと**(ログオン契機で上がってしまい、
検証にならない)。開催日は拒否される(VM を止めるので、その日が終わるため)。

```
○ 人が触らずに agent が上がりました(187秒)
```

これが出るまで、司令塔の自動化(timer)を有効にしないこと。

## その次: JV-Link が本当に動くか

agent が上がっても、**JV-Link まで通るか**は別。自動ログオンのセッションは
通常のログオンと同じはずだが、確かめていないものを前提にしない。

```
admin UI から windows 宛に `win-check` を投げ、結果だけで判断する
```

**RDP で繋いで確かめない。** 繋ぐと別の対話セッションができて、本番と条件が変わる。
