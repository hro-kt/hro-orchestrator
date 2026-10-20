"""外部への通知。★司令塔自身が落ちたことを、司令塔は報告できない。

2つに分ける:
  heartbeat … 1周を終えるたびに外部へ ping する。**止まったら外部が鳴る**。
               VM が落ちた/systemd が止まった/プロセスが死んだ、を拾える。
               (healthchecks.io / Better Stack などの「死活監視」URL を想定)
  alert     … 論理的な異常(agent が上がらない等)を能動的に送る。

★どちらも**失敗しても1周を壊さない**。通知のために開催日を落とすのは本末転倒。
★heartbeat は alert の有無に関わらず送る。目的が違う:
  heartbeat が途切れる = 司令塔が動いていない
  alert が来る         = 司令塔は動いているが、状況が悪い
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

TIMEOUT_SEC = 10.0


def _post(url: str, body: bytes | None, content_type: str | None) -> bool:
    headers = {"User-Agent": "hro-orchestrator"}
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
            return 200 <= getattr(r, "status", 200) < 300
    except Exception as e:      # noqa: BLE001 - 通知の失敗で開催日を落とさない
        log.warning("通知に失敗: %s (%s)", type(e).__name__, str(e)[:120])
        return False


def heartbeat(url: str, *, detail: str = "") -> bool:
    """生存 ping。★途切れたことを外部に気づかせるのが目的。"""
    if not url:
        return False
    return _post(url, detail.encode("utf-8")[:2000] if detail else None,
                 "text/plain; charset=utf-8" if detail else None)


def alert(url: str, message: str) -> bool:
    """異常の通知。Slack 互換の {"text": ...} で送る。"""
    if not url or not message:
        return False
    return _post(url, json.dumps({"text": message}, ensure_ascii=False).encode("utf-8"),
                 "application/json")
