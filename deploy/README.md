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
