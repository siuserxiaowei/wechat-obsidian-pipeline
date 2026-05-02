#!/usr/bin/env python3
"""wx_sync.py — 通过 jackwener/wx-cli 拉取微信聊天数据,转成 ChatLab JSON 格式.

为啥要这个脚本:
  - jackwener/wx-cli 是最稳的本地路径(不依赖 WeFlow GUI)
  - 它的输出格式是自定义的: {sender, type:"文本", time, timestamp, content}
  - 我们已有的 render.py / wf_report.py 期望 ChatLab 格式: {sender, accountName, type:0, timestamp, content}
  - 这里做 wx-cli → ChatLab 的格式转换 + 注入 group members 的 wxid → 昵称映射

输出: 与 WeFlow API 输出完全兼容的 ChatLab JSON, 直接喂给 render.py / wf_report.py.

用法:
  wx_sync.py <chat_id_or_name> [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                                 [--limit 50000] [--no-render]
                                 [--out PATH]

示例:
  # 拉天策成长团最近 7 天 + 渲染
  wx_sync.py 49192810916@chatroom --since 2026-04-25 --until 2026-05-02

  # 只拉数据不渲染(给 wf_report.py 后续用)
  wx_sync.py 49192810916@chatroom --since 2026-04-25 --no-render
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# ============================================================
# wx-cli 类型 → ChatLab 类型映射
#
# render.py 识别消息类型靠以下任一: type / localType / msg_type("xxx消息") / mediaType
# 我们尽量塞全, 让 render 不挑
# ============================================================

TYPE_MAP: dict[str, tuple[int, str, str | None]] = {
    # wx-cli 文字 → (ChatLab type, msg_type 中文, mediaType)
    "文本":       (0,     "文本消息",   None),
    "image":     (3,     "图片消息",   "image"),
    "图片":       (3,     "图片消息",   "image"),
    "voice":     (34,    "语音消息",   "voice"),
    "语音":       (34,    "语音消息",   "voice"),
    "video":     (43,    "视频消息",   "video"),
    "视频":       (43,    "视频消息",   "video"),
    "sticker":   (47,    "动画表情",   "emoji"),
    "表情":       (47,    "动画表情",   "emoji"),
    "动画表情":   (47,    "动画表情",   "emoji"),
    "location":  (48,    "位置消息",   None),
    "位置":       (48,    "位置消息",   None),
    "link":      (49,    "链接消息",   None),
    "链接":       (49,    "链接消息",   None),
    "分享":       (49,    "链接消息",   None),
    "链接/文件":  (49,    "链接/文件",  None),
    "文件":       (49,    "文件消息",   None),
    "file":      (49,    "文件消息",   None),
    "call":      (10000, "系统消息",   None),
    "system":    (10000, "系统消息",   None),
    "系统":       (10000, "系统消息",   None),
}


# ============================================================
# 调 wx-cli
# ============================================================

def wx_run(args: list[str]) -> str:
    cmd = ["wx"] + args
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raise RuntimeError(f"wx cli failed: {' '.join(cmd)}\nSTDERR: {p.stderr}")
    return p.stdout


def wx_history_json(chat: str, *, limit: int, since: str | None, until: str | None) -> list[dict]:
    args = ["history", chat, "-n", str(limit), "--json"]
    if since: args += ["--since", since]
    if until:
        # wx-cli 的 --until 是开区间(不含当天). 我们对外语义改成闭区间 → 自动 +1
        try:
            d = dt.datetime.strptime(until, "%Y-%m-%d")
            until_inclusive = (d + dt.timedelta(days=1)).strftime("%Y-%m-%d")
            args += ["--until", until_inclusive]
        except ValueError:
            args += ["--until", until]
    out = wx_run(args).strip()
    if not out: return []
    return json.loads(out)


def wx_members_json(chat: str) -> list[dict]:
    """非群聊抛错, 调用方应捕获."""
    out = wx_run(["members", chat, "--json"]).strip()
    if not out: return []
    return json.loads(out)


def lookup_chat_name(chat: str, *, weflow_snapshot: Path | None = None) -> str | None:
    """优先级:
       1. WeFlow sessions snapshot (有 displayName 全量映射)
       2. wx-cli sessions (一般只返回 chatroom_id, 但万一以后版本改了)
       3. wx-cli contacts (私聊场景)
    """
    # === 1) WeFlow sessions snapshot ===
    if weflow_snapshot and weflow_snapshot.exists():
        try:
            data = json.loads(weflow_snapshot.read_text(encoding="utf-8"))
            for s in data.get("sessions", []):
                if s.get("username") == chat:
                    name = s.get("displayName")
                    if name and name != chat:
                        return name
        except Exception:
            pass

    # === 2) wx-cli sessions ===
    try:
        out = wx_run(["sessions", "-n", "2000", "--json"]).strip()
        for s in json.loads(out):
            if s.get("username") == chat or s.get("chat") == chat:
                cn = s.get("chat") or s.get("name")
                if cn and cn != chat:
                    return cn
    except Exception:
        pass

    # === 3) wx-cli contacts (主要给私聊) ===
    if not chat.endswith("@chatroom"):
        try:
            # contacts -q 用 wxid 前缀做查询
            out = wx_run(["contacts", "-q", chat[:20], "-n", "5", "--json"]).strip()
            for c in (json.loads(out) if out else []):
                if c.get("username") == chat:
                    return c.get("display") or c.get("nickname") or chat
        except Exception:
            pass

    return None


# ============================================================
# 转换
# ============================================================

def normalize_message(m: dict, member_lookup: dict[str, dict]) -> dict:
    sender = m.get("sender") or ""
    raw_type = (m.get("type") or "").strip()
    cl_type, msg_type, media_type = TYPE_MAP.get(raw_type, (1, raw_type or "未知", None))

    member = member_lookup.get(sender, {})
    display = member.get("display") or sender or "Unknown"

    out = {
        "sender": sender,
        "accountName": display,
        "groupNickname": None,        # wx-cli 不区分群昵称, 用 display
        "timestamp": int(m.get("timestamp", 0)),
        "type": cl_type,
        "localType": cl_type,         # 给 render.py 多个 fallback
        "msg_type": msg_type,         # 同上
        "content": m.get("content", "") or "",
        "platformMessageId": str(m.get("local_id") or m.get("server_id") or ""),
    }
    if media_type:
        out["mediaType"] = media_type
    return out


def build_chatlab(
    chat: str,
    chat_name: str,
    is_group: bool,
    members: list[dict],
    messages: list[dict],
) -> dict:
    member_lookup = {m["username"]: m for m in members if m.get("username")}

    # ChatLab members 格式: {platformId, accountName, groupNickname, avatar}
    cl_members = [
        {
            "platformId": m.get("username", ""),
            "accountName": m.get("display", ""),
            "groupNickname": None,
            "avatar": None,
        }
        for m in members
    ]

    cl_messages = [normalize_message(m, member_lookup) for m in messages]
    cl_messages.sort(key=lambda m: m.get("timestamp", 0))

    return {
        "chatlab": {
            "version": "0.0.2",
            "exportedAt": int(time.time()),
            "generator": "wx-cli (jackwener) via wx_sync.py",
        },
        "meta": {
            "name": chat_name,
            "platform": "wechat",
            "type": "group" if is_group else "private",
            "groupId": chat if is_group else "",
            "ownerId": next((m.get("username") for m in members if m.get("is_owner")), ""),
        },
        "members": cl_members,
        "messages": cl_messages,
    }


# ============================================================
# CLI
# ============================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("chat", help="会话 ID(50341992009@chatroom 或 wxid_xxx) 或精确显示名")
    p.add_argument("--since", help="YYYY-MM-DD")
    p.add_argument("--until", help="YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=5000, help="最多拉多少条 (default: 5000, wx-cli 大 limit 会卡)")
    p.add_argument("--no-render", action="store_true", help="只生成 cache JSON, 不调 render.py")
    p.add_argument("--out", help="自定义输出路径(默认 .claudian/wechat/cache/)")
    p.add_argument("--secrets", default=None)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    # === 1) 调 wx history ===
    print(f"📥 wx history {args.chat} (limit={args.limit}, since={args.since}, until={args.until})")
    messages = wx_history_json(args.chat, limit=args.limit, since=args.since, until=args.until)
    print(f"   ✓ 拿到 {len(messages)} 条消息")
    if not messages:
        print("⚠️  没消息, 退出")
        return 3

    # === 2) 判定群聊还是私聊 ===
    is_group = "@chatroom" in args.chat

    # === 3) 拉群成员 (并发 wxid → display 映射) ===
    members: list[dict] = []
    if is_group:
        try:
            print(f"👥 wx members {args.chat}")
            members = wx_members_json(args.chat)
            print(f"   ✓ {len(members)} 个成员")
        except Exception as e:
            print(f"   ⚠️  拿成员失败: {e} (会用 wxid 显示)")

    # === 4) 找会话显示名 ===
    chat_name = args.chat
    here = Path(__file__).resolve().parent.parent
    weflow_snap = here / "cache" / "sessions-snapshot.json"
    name = lookup_chat_name(args.chat, weflow_snapshot=weflow_snap)
    if name:
        chat_name = name
    if chat_name == args.chat:
        print(f"   ⚠️  没找到 {args.chat} 的显示名, 用 chatroom_id 当目录名")
        print(f"      可手动跑 wf sessions 刷新 sessions-snapshot 后重试")

    # === 5) 转成 ChatLab JSON ===
    data = build_chatlab(args.chat, chat_name, is_group, members, messages)

    # === 6) 写到 cache/ (和 sync_paginated.py 同位置, 这样 wf_report 自动能找到) ===
    here = Path(__file__).resolve().parent.parent
    cache_dir = here / "cache"
    cache_dir.mkdir(exist_ok=True)
    if args.out:
        out_path = Path(args.out)
    else:
        safe = args.chat.replace("/", "_").replace("@", "_")
        out_path = cache_dir / f"{safe}-{int(time.time())}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 快照: {out_path}")
    print(f"   会话: {chat_name}")
    print(f"   类型: {'群聊' if is_group else '私聊'}")
    print(f"   消息: {len(messages)} 条")

    if args.no_render:
        print("(--no-render, 不渲染)")
        return 0

    # === 7) 调 render.py 出扁平 markdown(和 WeFlow 同结构) ===
    secrets = args.secrets or str(here / "secrets.json")
    render = here / "bin" / "render.py"
    vault = here.parent.parent  # vault 根目录

    print(f"📝 渲染 markdown ...")
    rc = subprocess.run([
        "python3", str(render), str(out_path),
        "--vault", str(vault),
        "--secrets", secrets,
    ]).returncode
    return rc


if __name__ == "__main__":
    sys.exit(main())
