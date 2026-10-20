# 配置手順(Linux 機 hro-calc-prod1)

★**自分が止める VM の上では動かせない。** だから常時起動が必要な Linux 機に置く
  (NAR 収集で毎日要るので、増設コストはゼロ)。

## 1. Azure の権限(マネージドID、最小権限)

```bash
# Linux VM にシステム割り当てマネージドID を付ける
az vm identity assign -g prod1 -n hro-calc-prod1

# 起動/停止だけのカスタムロールを作る(Contributor を付けない)
cat > vmpower.json <<'JSON'
{
  "Name": "HRO VM Power",
  "Description": "対象VMの起動/停止/状態参照のみ",
  "Actions": [
    "Microsoft.Compute/virtualMachines/read",
    "Microsoft.Compute/virtualMachines/instanceView/read",
    "Microsoft.Compute/virtualMachines/start/action",
    "Microsoft.Compute/virtualMachines/deallocate/action"
  ],
  "AssignableScopes": ["/subscriptions/<SUB_ID>/resourceGroups/prod1"]
}
JSON
az role definition create --role-definition vmpower.json

# ★割り当て先は**対象 VM だけ**。資源グループ全体にしない
PRINCIPAL=$(az vm show -g prod1 -n hro-calc-prod1 --query identity.principalId -o tsv)
SCOPE=$(az vm show -g prod1 -n hro-sync-prod2 --query id -o tsv)
az role assignment create --assignee "$PRINCIPAL" --role "HRO VM Power" --scope "$SCOPE"
```

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

## 4. ★死活監視(これが一番大事)

**司令塔自身が落ちたことは、司令塔には報告できない。**
VM が止まる / ディスクが埋まる / systemd が止まる。どれも「静かに何も起きない」
形になり、気づくのは夕方になる。これまで潰してきた失敗の形そのもの。

外部の死活監視サービス(healthchecks.io、Better Stack など)で URL を1つ作り、

```bash
echo 'ORC_HEARTBEAT_URL=https://hc-ping.com/xxxxxxxx' >> .env
```

猶予(grace)は **30分** を目安に。timer は10分ごとなので、2回落ちたら鳴る。

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
