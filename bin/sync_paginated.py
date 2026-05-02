#!/usr/bin/env python3
"""sync_paginated.py — 通过 ChatLab Pull 端点分页拉取一个会话的全部消息.

用法:
  sync_paginated.py <talker> [--start YYYYMMDD] [--end YYYYMMDD]
                              [--batch 500] [--max-retries 3]
                              [--no-render]
                              [--secrets PATH]

为什么需要这个:
  /api/v1/messages 一次拉太多会卡死 WeFlow.
  /api/v1/sessions/:id/messages (ChatLab Pull) 自带 sync.hasMore + nextOffset, 设计成分页用的.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any


def load_secrets(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def http_get_json(endpoint: str, path: str, token: str, timeout: int = 60) -> Any:
    url = endpoint.rstrip("/") + path
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_yyyymmdd(s: str | None) -> int | None:
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return int(datetime.strptime(s, "%Y%m%d").timestamp())
    if s.isdigit():
        return int(s)
    raise ValueError(f"无法解析时间: {s!r}")


def fetch_paginated(
    endpoint: str,
    token: str,
    talker: str,
    *,
    batch: int = 500,
    since: int | None = None,
    end: int | None = None,
    max_retries: int = 3,
    timeout: int = 90,
    on_progress=None,
) -> dict:
    """分页拉取所有消息. 用 /api/v1/messages?offset=N 翻页, 走 chatlab 格式输出.

    注: WeFlow 当前版本 ChatLab Pull (/api/v1/sessions/:id/messages) 返回 404,
    所以走更稳的 /api/v1/messages 端点.
    """
    offset = 0
    all_messages: list[dict] = []
    chatlab_meta: dict | None = None
    meta: dict | None = None
    members: list[dict] = []

    iteration = 0
    while True:
        iteration += 1
        params = {
            "talker": talker,
            "limit": batch,
            "offset": offset,
            "chatlab": "1",  # 输出 ChatLab 格式, 字段更完整
        }
        if since is not None: params["start"] = since
        if end is not None:   params["end"] = end
        path = "/api/v1/messages?" + urllib.parse.urlencode(params)

        last_err = None
        for attempt in range(max_retries):
            try:
                data = http_get_json(endpoint, path, token, timeout=timeout)
                last_err = None
                break
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = e
                wait = 2 ** attempt * 3  # 3,6,12 秒
                if on_progress:
                    on_progress(f"  ⚠️  第 {iteration} 批失败 (尝试 {attempt+1}/{max_retries}): {e}, 等 {wait}s 重试")
                time.sleep(wait)
        if last_err is not None:
            raise RuntimeError(f"分页拉取失败: {last_err}")

        # ChatLab 格式 vs 原始格式都可能出现, 兼容处理
        if chatlab_meta is None:
            chatlab_meta = data.get("chatlab", {})
        if meta is None:
            meta = data.get("meta", {})
        if not members:
            members = data.get("members", []) or []

        msgs = data.get("messages") or []
        all_messages.extend(msgs)

        # /api/v1/messages 用顶层 hasMore (不在 sync 块里)
        has_more = data.get("hasMore", False)
        count_in_batch = data.get("count") or len(msgs)

        if on_progress:
            on_progress(f"  ✓ 第 {iteration} 批: {count_in_batch} 条 (累计 {len(all_messages)})  hasMore={has_more}")

        if not has_more or not msgs:
            break
        offset += len(msgs)  # 实际收到多少条就跳多少

        # 轻微限速, 别打死 WeFlow
        time.sleep(0.5)

    return {
        "chatlab": chatlab_meta or {"version": "0.0.2", "exportedAt": int(time.time()), "generator": "WeFlow"},
        "meta": meta or {"name": talker, "platform": "wechat", "type": "group" if "@chatroom" in talker else "private", "groupId": talker},
        "members": members,
        "messages": all_messages,
        "sync": {"hasMore": False, "fetched_total": len(all_messages)},
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("talker")
    p.add_argument("--start", default=None, help="YYYYMMDD 或秒级时间戳")
    p.add_argument("--end", default=None, help="YYYYMMDD 或秒级时间戳")
    p.add_argument("--batch", type=int, default=500)
    p.add_argument("--max-retries", type=int, default=3)
    p.add_argument("--secrets", default=None)
    p.add_argument("--no-render", action="store_true")
    p.add_argument("--vault", default=".")
    p.add_argument("--out", default=None, help="保存的 JSON 路径(默认 cache 自动命名)")
    args = p.parse_args(argv)

    if args.secrets is None:
        # 默认从同目录上层找 secrets.json
        here = Path(__file__).resolve().parent.parent
        args.secrets = str(here / "secrets.json")
    secrets = load_secrets(Path(args.secrets))

    endpoint = secrets["endpoint"]
    token = secrets["token"]

    since = parse_yyyymmdd(args.start)
    end = parse_yyyymmdd(args.end)
    if end is not None and len(str(args.end)) == 8:
        # 默认 end 扩展到当天 23:59:59
        end += 86399

    print(f"📥 分页拉取 {args.talker}")
    print(f"   batch={args.batch}, since={since}, end={end}")

    result = fetch_paginated(
        endpoint, token, args.talker,
        batch=args.batch,
        since=since, end=end,
        max_retries=args.max_retries,
        on_progress=print,
    )

    total = len(result["messages"])
    print(f"✅ 拉取完毕, 共 {total} 条消息")

    # 保存快照
    cache_dir = Path(__file__).resolve().parent.parent / "cache"
    cache_dir.mkdir(exist_ok=True)
    if args.out:
        snap = Path(args.out)
    else:
        safe = args.talker.replace("/", "_").replace("@", "_")
        snap = cache_dir / f"{safe}-{int(time.time())}.json"
    snap.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 快照: {snap}")

    if args.no_render or total == 0:
        return 0

    # 调 render.py
    render_py = Path(__file__).resolve().parent / "render.py"
    print(f"📝 渲染中...")
    import subprocess
    rc = subprocess.run([
        "python3", str(render_py), str(snap),
        "--vault", args.vault,
        "--secrets", args.secrets,
    ]).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
