#!/usr/bin/env python3
"""render.py — WeFlow JSON → Obsidian Markdown

支持三种 JSON 格式自动识别:
  1. WeFlow 文件导出格式 (顶层有 `weflow` + `session` + `messages`)
  2. WeFlow API 原始格式  (顶层 `success` + `talker` + `messages`)
  3. ChatLab 格式         (顶层 `chatlab` + `meta` + `members` + `messages`)

输出: 每个会话每天一个 .md, 加一个 _index.md 索引.

用法:
  render.py <input.json> [--vault PATH] [--secrets PATH]
  render.py <input.json> --to <output_dir>          自定义输出目录
  render.py <input.json> --stdout                   输出到 stdout (调试用)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ============================================================
# 格式识别 + 标准化
# ============================================================

def detect_format(data: dict) -> str:
    if "chatlab" in data and "meta" in data:
        return "chatlab"
    if "weflow" in data and "session" in data:
        return "weflow_export"
    if "success" in data and "talker" in data and "messages" in data:
        return "weflow_api"
    raise ValueError("无法识别 JSON 格式 (期望 chatlab/weflow_export/weflow_api 之一)")


def normalize(data: dict) -> dict:
    """把任意输入统一成内部表示"""
    fmt = detect_format(data)
    if fmt == "weflow_export":
        s = data.get("session", {})
        return {
            "format": fmt,
            "chat_id": s.get("wxid", "unknown"),
            "chat_name": s.get("displayName") or s.get("nickname") or s.get("wxid"),
            "chat_type": "group" if s.get("type") == "群聊" else "private",
            "exported_at": data.get("weflow", {}).get("exportedAt"),
            "messages": data.get("messages", []),
        }
    if fmt == "weflow_api":
        return {
            "format": fmt,
            "chat_id": data.get("talker"),
            "chat_name": data.get("talker"),
            "chat_type": "group" if "@chatroom" in (data.get("talker") or "") else "private",
            "exported_at": int(datetime.now(timezone.utc).timestamp()),
            "messages": data.get("messages", []),
        }
    # chatlab
    meta = data.get("meta", {})
    return {
        "format": fmt,
        "chat_id": meta.get("groupId") or meta.get("id") or "unknown",
        "chat_name": meta.get("name", "unknown"),
        "chat_type": meta.get("type", "private"),
        "exported_at": data.get("chatlab", {}).get("exportedAt"),
        "members": data.get("members", []),
        "messages": data.get("messages", []),
    }


# ============================================================
# 文件名安全化 + 路径
# ============================================================

_UNSAFE = re.compile(r'[\\/:*?"<>|]')

def safe_name(name: str, fallback: str = "unknown") -> str:
    if not name:
        return fallback
    s = _UNSAFE.sub("_", name).strip().strip(".")
    return s or fallback


# ============================================================
# 消息类型解析
# ============================================================

def msg_sender_label(m: dict, fmt: str, member_lookup: dict) -> str:
    """优先级: 群昵称 > 备注 > 显示名 > wxid"""
    if fmt == "chatlab":
        nick = m.get("groupNickname") or m.get("accountName") or m.get("sender")
        return nick or "?"
    nick = m.get("senderDisplayName") or m.get("senderUsername") or "?"
    return nick


def msg_timestamp(m: dict, fmt: str) -> int:
    if fmt == "chatlab":
        return int(m.get("timestamp", 0))
    return int(m.get("createTime", 0))


def msg_render(m: dict, fmt: str, media_strategy: str, media_root: str) -> str:
    """单条消息渲染为 Markdown 片段(不含时间和发言人, 仅 body)"""
    content = (m.get("content") or m.get("rawContent") or "").strip()
    local_type = m.get("localType")
    msg_type = m.get("type", "")

    # === 链接卡片 (公众号/小程序) ===
    if m.get("appMsgKind") == "link" or m.get("appMsgType") == "5":
        title = m.get("linkTitle") or content
        url = m.get("linkUrl", "")
        source = m.get("appMsgSourceName", "")
        out = f"> [!quote]+ 链接卡片"
        if source:
            out += f" · {source}"
        out += "\n"
        if url:
            out += f"> **[{title}]({url})**\n"
        else:
            out += f"> **{title}**\n"
        return out

    # === 图片 ===
    if local_type == 3 or msg_type == "图片消息" or m.get("mediaType") == "image":
        fname = m.get("mediaFileName") or m.get("mediaPath") or ""
        if fname and media_strategy in ("link", "copy"):
            return f"![[{media_root}/{fname}]]"
        return "🖼️ [图片]"

    # === 语音 ===
    if local_type == 34 or msg_type == "语音消息" or m.get("mediaType") == "voice":
        fname = m.get("mediaFileName") or m.get("mediaPath") or ""
        if fname and media_strategy in ("link", "copy"):
            return f"🎙️ ![[{media_root}/{fname}]]"
        return "🎙️ [语音]"

    # === 视频 ===
    if local_type == 43 or msg_type == "视频消息" or m.get("mediaType") == "video":
        fname = m.get("mediaFileName") or m.get("mediaPath") or ""
        if fname and media_strategy in ("link", "copy"):
            return f"🎬 ![[{media_root}/{fname}]]"
        return "🎬 [视频]"

    # === 表情 ===
    if local_type == 47 or msg_type == "动画表情" or m.get("mediaType") == "emoji":
        return "😀 [表情]"

    # === 文件 ===
    if msg_type == "文件消息" or m.get("appMsgKind") == "file":
        title = m.get("appMsgTitle") or content
        return f"📎 **[文件]** {title}"

    # === 系统/撤回/邀请 ===
    if local_type in (10000, 10002) or msg_type == "系统消息":
        return f"> [!note]- 系统\n> {content}"

    # === 引用回复 ===
    if local_type == 49 and m.get("appMsgKind") == "refer":
        ref = m.get("appMsgReferContent") or ""
        body = m.get("appMsgTitle") or content
        return f"> [!quote]- 回复\n> {ref}\n\n{body}"

    # === 文本(默认) ===
    if content:
        # Obsidian 兼容: 不破坏列表、引用. 简单做: 转义可能误触的开头字符
        return content.replace("\n", "  \n")  # 保留换行
    return f"_({msg_type or local_type or '未知消息'})_"


# ============================================================
# 渲染主流程
# ============================================================

def render_day(
    day: str,
    msgs: list[dict],
    chat_name: str,
    chat_id: str,
    chat_type: str,
    fmt: str,
    media_strategy: str,
    media_root: str,
    member_lookup: dict,
) -> str:
    """渲染单日的 Markdown"""
    chat_type_zh = "群聊" if chat_type == "group" else "私聊"
    safe_chat = safe_name(chat_name)
    tag_chat = re.sub(r'[\s/]+', '-', chat_name.lower())
    fm = [
        "---",
        "type: chat-export",
        f"chat: {chat_name}",
        f'chat_id: "{chat_id}"',
        f"chat_type: {chat_type}",
        f"date: {day}",
        f"message_count: {len(msgs)}",
        f"exported_at: {datetime.now().isoformat(timespec='seconds')}",
        f"source: weflow",
        f"tags: [chat, wechat, {chat_type}, {tag_chat}]",
        "---",
        "",
        f"# 💬 {chat_name} · {day}",
        "",
        f"> [!info]+ 元数据",
        f"> - **会话**: {chat_name} (`{chat_id}`)",
        f"> - **类型**: {chat_type_zh}",
        f"> - **日期**: {day}",
        f"> - **消息数**: {len(msgs)}",
        "",
        "## 时间线",
        "",
    ]

    # 按发言人聚合连续消息(同一分钟、同一发言人合并)
    last_sender = None
    last_minute = None
    for m in msgs:
        ts = msg_timestamp(m, fmt)
        if not ts:
            continue
        dt = datetime.fromtimestamp(ts)
        minute = dt.strftime("%H:%M")
        sender = msg_sender_label(m, fmt, member_lookup)
        body = msg_render(m, fmt, media_strategy, media_root)

        if sender == last_sender and minute == last_minute:
            fm.append(body)
        else:
            fm.append("")
            fm.append(f"### {minute} {sender}")
            fm.append("")
            fm.append(body)
            last_sender = sender
            last_minute = minute

    fm.append("")
    return "\n".join(fm)


def scan_existing_days(out_chat_dir: Path) -> dict[str, int]:
    """扫描目录里所有已存在的 YYYY-MM-DD.md, 从 frontmatter 读 message_count.
    用于 _index 聚合, 防止单次 sync 覆盖历史数据.
    """
    found: dict[str, int] = {}
    pat = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
    if not out_chat_dir.exists():
        return found
    for p in out_chat_dir.iterdir():
        if not pat.match(p.name):
            continue
        day = p.stem
        n = 0
        try:
            content = p.read_text(encoding="utf-8")
            m = re.search(r'^message_count:\s*(\d+)', content, re.M)
            if m:
                n = int(m.group(1))
        except Exception:
            pass
        found[day] = n
    return found


def render_index(
    chat_name: str,
    chat_id: str,
    chat_type: str,
    by_day: dict,
    out_chat_dir: Path,
) -> str:
    # 合并: 本次 sync 的 + 已有的
    aggregated: dict[str, int] = {}
    for day, msgs in by_day.items():
        aggregated[day] = len(msgs)
    for day, n in scan_existing_days(out_chat_dir).items():
        if day not in aggregated:
            aggregated[day] = n

    chat_type_zh = "群聊" if chat_type == "group" else "私聊"
    days = sorted(aggregated.keys())
    total = sum(aggregated.values())
    tag_chat = re.sub(r'[\s/]+', '-', chat_name.lower())
    lines = [
        "---",
        "type: chat-index",
        f"chat: {chat_name}",
        f'chat_id: "{chat_id}"',
        f"chat_type: {chat_type}",
        f"date_range: {days[0] if days else '-'} → {days[-1] if days else '-'}",
        f"day_count: {len(days)}",
        f"message_count: {total}",
        f"tags: [chat-index, wechat, {tag_chat}]",
        "---",
        "",
        f"# 📂 {chat_name}",
        "",
        f"> [!info]",
        f"> - **类型**: {chat_type_zh}",
        f"> - **ID**: `{chat_id}`",
        f"> - **日期范围**: {days[0] if days else '-'} → {days[-1] if days else '-'}",
        f"> - **天数**: {len(days)}",
        f"> - **消息总数**: {total}",
        "",
        "## 按日索引",
        "",
        "| 日期 | 消息数 | 链接 |",
        "|---|---:|---|",
    ]
    for d in days:
        n = aggregated[d]
        lines.append(f"| {d} | {n} | [[{out_chat_dir.name}/{d}\\|→ 查看]] |")
    lines.append("")
    lines.append("## Dataview 视图(每日条数)")
    lines.append("")
    lines.append("```dataview")
    lines.append("TABLE date AS 日期, message_count AS 消息数, file.mtime AS 更新")
    lines.append('FROM "聊天记录导出"')
    lines.append(f'WHERE chat = "{chat_name}" AND type = "chat-export"')
    lines.append("SORT date DESC")
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def write_files(
    norm: dict,
    vault_root: Path,
    export_root: str,
    media_strategy: str,
) -> dict:
    chat_id = norm["chat_id"]
    chat_name = norm["chat_name"]
    chat_type = norm["chat_type"]
    fmt = norm["format"]
    msgs = norm["messages"]

    # 群成员表(用于 ChatLab 显示名)
    member_lookup = {}
    for m in norm.get("members", []):
        member_lookup[m.get("platformId")] = m

    # 按日分组
    by_day: dict[str, list] = defaultdict(list)
    for m in msgs:
        ts = msg_timestamp(m, fmt)
        if not ts:
            continue
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        by_day[day].append(m)

    # 输出目录
    safe_chat = safe_name(chat_name)
    chat_dir_name = "群聊" if chat_type == "group" else "私聊"
    out_dir = vault_root / export_root / chat_dir_name / safe_chat
    out_dir.mkdir(parents=True, exist_ok=True)

    # 媒体根目录(相对于 vault)
    media_root = f"{export_root}/_media/{chat_id}"

    # 写每日文件
    written = []
    for day, day_msgs in sorted(by_day.items()):
        # 时间排序
        day_msgs.sort(key=lambda m: msg_timestamp(m, fmt))
        md = render_day(
            day, day_msgs, chat_name, chat_id, chat_type, fmt,
            media_strategy, media_root, member_lookup,
        )
        out_file = out_dir / f"{day}.md"
        out_file.write_text(md, encoding="utf-8")
        written.append(out_file)

    # 写索引
    index = render_index(chat_name, chat_id, chat_type, by_day, out_dir)
    (out_dir / "_index.md").write_text(index, encoding="utf-8")

    return {
        "out_dir": str(out_dir),
        "days": len(by_day),
        "messages": len(msgs),
        "files": [str(p.relative_to(vault_root)) for p in written],
    }


# ============================================================
# CLI
# ============================================================

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="JSON 文件路径")
    p.add_argument("--vault", default=".", help="vault 根目录")
    p.add_argument("--secrets", default=None, help="secrets.json 路径(读取 export_root + media_strategy)")
    p.add_argument("--to", default=None, help="自定义输出目录(覆盖 secrets 配置)")
    p.add_argument("--stdout", action="store_true", help="输出到 stdout(只渲染第一天)")
    args = p.parse_args(argv)

    inp = Path(args.input)
    if not inp.exists():
        print(f"❌ 找不到 {inp}", file=sys.stderr)
        return 1

    data = json.loads(inp.read_text(encoding="utf-8"))
    norm = normalize(data)

    # 读 secrets 配置
    export_root = "聊天记录导出"
    media_strategy = "link"
    if args.secrets and Path(args.secrets).exists():
        cfg = json.loads(Path(args.secrets).read_text(encoding="utf-8"))
        export_root = cfg.get("export_root", export_root)
        media_strategy = cfg.get("media_strategy", media_strategy)

    if args.stdout:
        # 只渲染所有消息合在一起做预览
        days = defaultdict(list)
        for m in norm["messages"]:
            ts = msg_timestamp(m, norm["format"])
            if ts:
                day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
                days[day].append(m)
        if days:
            first_day = sorted(days.keys())[0]
            md = render_day(
                first_day, days[first_day],
                norm["chat_name"], norm["chat_id"], norm["chat_type"],
                norm["format"], media_strategy, "_media", {},
            )
            print(md)
        return 0

    vault = Path(args.vault).resolve()
    result = write_files(norm, vault, export_root, media_strategy)

    print(f"✅ 渲染完毕")
    print(f"   会话: {norm['chat_name']} ({norm['chat_type']})")
    print(f"   目录: {result['out_dir']}")
    print(f"   天数: {result['days']}, 消息数: {result['messages']}")
    print(f"   生成文件: {len(result['files'])} 个 + _index.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
