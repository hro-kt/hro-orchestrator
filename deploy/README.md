# 配置手順(Linux 機 hro-calc-prod1)

★**自分が止める VM の上では動かせない。** だから常時起動が必要な Linux 機に置く
  (NAR 収集で毎日要るので、増設コストはゼロ)。

## 1. Azure の権限(マネージドID、最小権限)

★**この作業は VM 上ではできない。** ID を付ける操作そのものに認証が要るので、
  **手元の PC か Azure Cloud Shell**(`az login` 済み)から行う。
★カスタムロールの作成には **所有者** か **ユーザー アクセス管理者** の権限が要る。
  持っていない場合は末尾の「権限が足りないとき」を読むこと。

### 1-1. マネージドID を付ける

```bash
az login
az account set --subscription <SUB_ID>
az vm identity assign -g prod1 -n hro-calc-prod1       # 司令塔が動く機
```

### 1-2. 起動/停止だけのカスタムロールを作る

Contributor は付けない(VM を消す権限まで渡ることになる)。

```bash
SUB=$(az account show --query id -o tsv)

cat > vmpower.json <<JSON
{
  "Name": "HRO VM Power",
  "Description": "対象VMの起動/停止/状態参照のみ",
  "Actions": [
    "Microsoft.Compute/virtualMachines/read",
    "Microsoft.Compute/virtualMachines/instanceView/read",
    "Microsoft.Compute/virtualMachines/start/action",
    "Microsoft.Compute/virtualMachines/deallocate/action"
  ],
  "NotActions": [],
  "AssignableScopes": ["/subscriptions/$SUB"]
}
JSON

az role definition create --role-definition vmpower.json
```

PowerShell なら(ヒアドキュメントが使えないので):

```powershell
$SUB = az account show --query id -o tsv
@"
{
  "Name": "HRO VM Power",
  "Description": "対象VMの起動/停止/状態参照のみ",
  "Actions": [
    "Microsoft.Compute/virtualMachines/read",
    "Microsoft.Compute/virtualMachines/instanceView/read",
    "Microsoft.Compute/virtualMachines/start/action",
    "Microsoft.Compute/virtualMachines/deallocate/action"
  ],
  "NotActions": [],
  "AssignableScopes": ["/subscriptions/$SUB"]
}
"@ | Set-Content -Encoding utf8 vmpower.json

az role definition create --role-definition vmpower.json
```

★`AssignableScopes` は「このロールを**割り当てられる範囲**」であって、
  実際に効く範囲ではない。実際の範囲は次の割り当てで決まる。
★作成直後は**名前での解決が数分効かない**。次の割り当てで
  「Role 'HRO VM Power' doesn't exist.」と出ても、作成は成功している。
  名前ではなく ID で指定すれば待たずに済む(次節)。

### 1-3. 対象 VM **だけ**に割り当てる

```bash
PRINCIPAL=$(az vm show -g prod1 -n hro-calc-prod1 --query identity.principalId -o tsv)
SCOPE=$(az vm show -g prod1 -n hro-sync-prod2 --query id -o tsv)   # 止めたい機
[ -n "$PRINCIPAL" ] || echo "✗ マネージドID が付いていません(1-1 をやり直す)"

# ★ロールは**名前ではなく ID** で指定する。作成直後は名前での解決が数分効かず、
#   「Role 'HRO VM Power' doesn't exist.」になる(作成自体は成功している)。
ROLE_ID=$(az role definition list --name "HRO VM Power" --query "[0].id" -o tsv)
until [ -n "$ROLE_ID" ]; do
  echo "ロールの反映を待っています…"; sleep 20
  ROLE_ID=$(az role definition list --name "HRO VM Power" --query "[0].id" -o tsv)
done

az role assignment create --assignee-object-id "$PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "$ROLE_ID" --scope "$SCOPE"
```

★`--assignee` ではなく `--assignee-object-id` + `--assignee-principal-type` を
  使う。マネージドID は Graph から引けないことがあり、`--assignee` だと
  「Principal not found」になる。
★スコープは**対象 VM の id**。資源グループ全体にしない。

### 1-4. 確かめる

```bash
az role assignment list --assignee "$PRINCIPAL" --scope "$SCOPE" -o table
```

そのうえで Linux 機に戻り:

```bash
az login --identity
az account show
az vm get-instance-view -g prod1 -n hro-sync-prod2 \
  --query "instanceView.statuses[?starts_with(code,'PowerState/')].displayStatus" -o tsv
```

最後のコマンドで `VM running` / `VM deallocated` が返れば、権限まで通っている。

### 権限が足りないとき

カスタムロールを作れない(所有者でない)なら、組み込みロールで代用できる。

```bash
az role assignment create --assignee-object-id "$PRINCIPAL" \
  --assignee-principal-type ServicePrincipal \
  --role "Virtual Machine Contributor" --scope "$SCOPE"
```

★こちらは**その VM の削除や変更もできてしまう**。スコープが1台に限られて
  いるとはいえ、カスタムロールより緩い。使うなら、後でカスタムロールに
  差し替えること。

## 2. 設置

```bash
cd ~/hro && git clone https://github.com/hro-kt/hro-orchestrator.git
cd hro-orchestrator && poetry install
cp .env.example .env && chmod 600 .env   # ★編集すること
az login --identity                      # マネージドID でログイン

# 何をするつもりかだけ見る(副作用なし)
poetry run hro-orchestrator status
poetry run hro-orchestrator plan
poetry run hro-orchestrator run --dry-run
```

## 3. 自動化(**数回の開催で判断を確かめてから**)

```bash
sudo cp deploy/hro-orchestrator.service deploy/hro-orchestrator.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hro-orchestrator.timer
systemctl list-timers hro-orchestrator.timer
```

★いきなり自動にしない。まず `plan` を毎日見て、**起こす時刻・投入する時刻・
  止める時刻が意図どおりか**を確かめる。電源の判断を間違えると一日を失う。

## 3.5 ★Windows の agent が「ログイン無し」で上がるか

**自動化の前提**。`az vm start` は Windows を起こすが**誰もログインしない**。
agent がログイン契機で起動する設定のままだと、VM は立つのに agent は上がらない。

★「ログオンしていなくても実行」に変えてはいけない。Session 0(非対話)で走るので
**JV-Link が動かない**(対話的デスクトップを要求する。2026-09 確認)。
正解は**自動ログオン**で、agent の起動方法は変えない。

設定と検証の手順は `deploy/WINDOWS_AGENT.md`。非開催日に

```bash
poetry run hro-orchestrator coldstart-test
```

を通して `○ 人が触らずに agent が上がりました` を見るまで、timer を
有効にしないこと。

## 3.7 朝の支度(sync-all を人が流さなくていいようにする)

開催日は、第1発走の **6 時間前**に VM を起こし、agent が応答したら `sync_all` を
投入して、終わるまで `race_day` を入れずに待つ。

**なぜ待つのか。** JV-Link は1台1プロセル。`sync_all` が走ったまま `race_day` が
始まると、`race_day` は `run_odds` を**見送って先へ進む**
(`hro_operations/orchestrator.py` の `_run_step` / `_jvlink_busy`)。
見送られると速報オッズが一日来ないので、**1件も買えない**。黙って0件になる壊れ方で、
一番高くつく。

締切は2段:

| 既定 | 時刻(第1発走10:00なら) | 何をするか |
|---|---|---|
| `morning_lead_hours=6.0` | 04:00 | VM を起こす |
| `morning_cutoff_hours=3.0` | 07:00 | これ以降は**新しく始めない**(始めても終わらない) |
| `morning_last_call_hours=2.5` | 07:30 | これ以降も走っていたら**打ち切る** |
| `enqueue_lead_hours=2.0` | 08:00 | `race_day` を投入 |

打ち切りは乱暴に見えるが、残すほうが高くつく。同期は `SYNC_SMART` が DB の
frontier から続きを取り直すので、打ち切っても失わない。発注は失うと戻らない。

**`morning_lead_hours=6.0` は暫定値。** 実測で決め直すこと:

```sql
SET TIME ZONE 'Asia/Tokyo';
SELECT id, requested_at,
       finished_at - started_at AS duration, status
  FROM ops_job
 WHERE kind = 'sync_all' AND started_at IS NOT NULL
 ORDER BY id DESC LIMIT 20;
```

所要の最大値 + 1時間を `ORC_MORNING_LEAD_HOURS` に入れる。

```bash
# 効きすぎたら、コードを触らずに止められる
echo 'ORC_MORNING_JOBS=' >> .env          # 空 = 朝の支度をしない(従来の挙動)
echo 'ORC_MORNING_LEAD_HOURS=8' >> .env   # もっと早く起こす
```

★順序(`lead ≥ cutoff ≥ last_call ≥ enqueue`)が崩れた設定は起動時に例外で落ちる。
「起こす前に締切」「打ち切る前に投入」を走らせない。

## 4. ★死活監視(これが一番大事)

**司令塔自身が落ちたことは、司令塔には報告できない。**
VM が止まる / ディスクが埋まる / systemd が止まる。どれも「静かに何も起きない」
形になり、気づくのは夕方になる。これまで潰してきた失敗の形そのもの。

外部の死活監視サービス(healthchecks.io、Better Stack など)で URL を1つ作り、

```bash
echo 'ORC_HEARTBEAT_URL=https://hc-ping.com/xxxxxxxx' >> .env
```

healthchecks.io の設定:

| 項目 | 値 | 理由 |
|---|---|---|
| **Period** | **10分** | timer が10分ごとなので、ping の間隔もそれに合わせる |
| **Grace Time** | **30分** | 合計40分で鳴る。3回連続で落ちたら、の意味。
  DB の一瞬の再起動(1〜2分)では鳴らない |

開催日は第1発走の3時間前に VM を起こすので、40分で気づければ
**2時間20分の余裕**が残る。手で起こし直せる。

★「動いていない」と「動いているが異常」の両方をここで拾う。
  論理的な異常(agent が上がらない等)があった周回は `<url>/fail` を叩くので、
  **待たずに即座に鳴る**。別の通知先を用意しなくてよい。

これが無いと、`hro-orchestrator` を入れたこと自体が
**「一日を失う新しい経路」を増やしただけ**になる。

## なぜ hro-calc-prod1 に置くのか

DB が VNet 内の私設 PostgreSQL なので、素のサーバレス(Functions / Automation)
からは届かない。司令塔は開催日判定・agent の生死・ジョブ投入のすべてで DB を
使うので、**VNet の中にいる必要がある**。

| 候補 | 可否 | 備考 |
|---|---|---|
| hro-calc-prod1 | ◎ | 既に VNet 内。NAR 収集で常時起動。追加コスト0 |
| hro-admin-prod1 | ○ | 同じく VNet 内。負荷は軽い |
| Azure Functions | △ | VNet 統合に Premium/Flex が要る |
| Automation Account | △ | DB に届かせるには Hybrid Worker(=結局 VM) |
| Container Apps Job | ○ | VNet 可。イメージの構築・配備が増える |

置き場所より**死活監視の有無**の方が効く。どこに置いても VM は落ちる。
