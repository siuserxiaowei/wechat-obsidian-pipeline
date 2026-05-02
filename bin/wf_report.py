#!/usr/bin/env python3
"""wf_report.py — 把 WeFlow JSON 快照适配成 wechat-daily-report skill 需要的输入.

用法:
  wf_report.py <talker> --date YYYY-MM-DD [--workdir PATH] [--secrets PATH]
                                          [--auto-sync]  [--source FILE]

输出 (workdir 默认: 临时目录):
  stats.json
  simplified_chat.txt  (或 _1, _2 多文件)
  meta.json            (额外: 给后续脚本用的元信息: talker / chat_name / vault_path)

为啥:
  原 skill 走 vendor/wechat-decrypt 路径解密 SQLite 拿数据.
  我们已经有 WeFlow API 拿到的 ChatLab JSON 快照, 直接复用更省事.

调用流程 (替代原 SKILL.md 步骤 1-4):
  ① 从 .claudian/wechat/cache/ 找到 talker 的最新快照
     如果没有 / 旧了, 加 --auto-sync 自动调 sync_paginated.py 拉新的
  ② 按 --date 过滤当天消息
  ③ 复用 analyze_chat.py 里的 top_talker / night_owl / word_cloud / simplified_text 逻辑
  ④ 写 stats.json + simplified_chat.txt
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False
    print("⚠️  jieba 没装, 词云会很弱. 进 venv: source ~/.claude/skills/wechat-daily-report/.venv/bin/activate", file=sys.stderr)

# ============================================================
# Stopwords (从 analyze_chat.py 复制, 完全一致)
# ============================================================

WORD_CLOUD_STOPWORDS = set([
    '的','了','我','是','你','在','他','我们','好','去','都','就','那','有',
    '这','也','要','吗','啊','吧','呢','哈','哈哈','哈哈哈','图片','表情',
    '动画表情','语音','转文字','语音转文字','链接','分享','回复','一条','一张',
    '发的','说'
])

TALKER_STOPWORDS = WORD_CLOUD_STOPWORDS | set([
    '一个','这个','那个','什么','怎么','可以','就是','不是','没有','还有',
    '但是','现在','知道','真的','感觉','觉得','可能','应该','已经','还是','一下'
])

NIGHT_HOURS = lambda h: h >= 23 or h < 6

WORD_CLOUD_COLORS = ["#07C160","#576B95","#FA5151","#FFD200","#333333","#888888","#1AAD19","#2782D7"]


# ============================================================
# 辅助
# ============================================================

def get_display_name(msg: dict) -> str:
    return msg.get('groupNickname') or msg.get('accountName') or 'Unknown'


def fmt_ts(ts: int):
    dt = datetime.datetime.fromtimestamp(ts)
    return dt.strftime('%Y-%m-%d %H:%M:%S'), dt


# ============================================================
# 找快照
# ============================================================

def find_latest_snapshot(cache_dir: Path, talker: str) -> Path | None:
    safe = talker.replace("/", "_").replace("@", "_")
    candidates = sorted(
        cache_dir.glob(f"{safe}-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def auto_sync(talker: str, secrets_path: Path, *, date: str | None = None, prefer: str = "wx") -> Path:
    """自动拉一份新快照. 默认走 wx-cli, 失败回退 WeFlow.

    prefer: "wx" (默认) 用 jackwener/wx-cli; "weflow" 用 sync_paginated.py
    date: 如果给了, 拉那一天的数据(更窄, 更快); 否则只用 since=今天-30天 兜个底
    """
    here = Path(__file__).resolve().parent
    cache_dir = secrets_path.parent / "cache"

    # 时间窗口: 单日 -> 那一天; 否则最近 30 天
    if date:
        since = until = date
    else:
        d = datetime.datetime.now()
        since = (d - datetime.timedelta(days=30)).strftime("%Y-%m-%d")
        until = d.strftime("%Y-%m-%d")

    if prefer == "wx":
        # 检查 wx 命令是否可用
        if subprocess.run(["which", "wx"], capture_output=True).returncode == 0:
            cmd = [
                "python3", str(here / "wx_sync.py"),
                talker,
                "--since", since,
                "--until", until,
                "--no-render",
                "--secrets", str(secrets_path),
            ]
            print(f"🔄 自动同步 {talker} (wx-cli, {since} → {until})")
            rc = subprocess.run(cmd).returncode
            if rc == 0:
                snap = find_latest_snapshot(cache_dir, talker)
                if snap:
                    return snap
            print(f"   ⚠️  wx-cli sync 没产出, 回退到 WeFlow")

    # WeFlow 兜底
    cmd = [
        "python3", str(here / "sync_paginated.py"),
        talker,
        "--batch", "500",
        "--no-render",
        "--secrets", str(secrets_path),
    ]
    print(f"🔄 自动同步 {talker} (WeFlow API)")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        raise RuntimeError("两种 sync 都失败了")
    snap = find_latest_snapshot(cache_dir, talker)
    if not snap:
        raise RuntimeError("sync 后还是没找到快照")
    return snap


# ============================================================
# 过滤当天 + 适配为 skill 期待的格式
# ============================================================

def filter_by_date(data: dict, date_str: str) -> dict:
    """date_str = YYYY-MM-DD, 返回当天的 data 子集."""
    day_start = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    start_ts = int(day_start.timestamp())
    end_ts = start_ts + 86400

    msgs = [
        m for m in data.get("messages", [])
        if start_ts <= int(m.get("timestamp", 0)) < end_ts
    ]
    return {
        "chatlab": data.get("chatlab", {}),
        "meta": data.get("meta", {}),
        "members": data.get("members", []),
        "messages": msgs,
    }


# ============================================================
# 头像映射 (从 analyze_chat.py 改写)
# ============================================================

def build_avatar_maps(data: dict, messages: list) -> tuple:
    members = data.get("members", []) or []
    sender_avatar_map = {}
    name_avatar_map = {}
    for member in members:
        sender = member.get("platformId")
        name = member.get("accountName")
        avatar = member.get("avatar")
        if sender and avatar:
            sender_avatar_map[sender] = avatar
        if name and avatar and name not in name_avatar_map:
            name_avatar_map[name] = avatar
    for msg in messages:
        sender = msg.get("sender")
        display_name = get_display_name(msg)
        avatar = sender_avatar_map.get(sender)
        if display_name and avatar and display_name not in name_avatar_map:
            name_avatar_map[display_name] = avatar
    return sender_avatar_map, name_avatar_map


def resolve_avatar_for_name(name, name_sender_counter, sender_avatar_map, fallback_name_avatar_map):
    counter = name_sender_counter.get(name)
    if counter:
        for sender, _ in counter.most_common():
            avatar = sender_avatar_map.get(sender)
            if avatar:
                return avatar
    return fallback_name_avatar_map.get(name)


# ============================================================
# 词云 (从 analyze_chat.py 复制, 接口不变)
# ============================================================

def generate_word_cloud_data(text_messages, top_n=60):
    contents = []
    for m in text_messages:
        content = m.get('content', '') or ''
        if content.startswith('[语音转文字] '):
            content = content[7:]
        contents.append(content)
    combined_text = " ".join(contents)
    words = []
    if JIEBA_AVAILABLE:
        for w in jieba.cut(combined_text):
            if len(w) > 1 and w not in WORD_CLOUD_STOPWORDS:
                words.append(w)
    word_counts = Counter(words)
    common_words = word_counts.most_common(top_n)
    cloud = []
    if not common_words:
        return cloud
    max_count = common_words[0][1]
    for word, count in common_words:
        size = min(40, max(12, 10 + (count / max_count) * 30))
        cloud.append({
            "text": word,
            "count": count,
            "size": int(size),
            "color": random.choice(WORD_CLOUD_COLORS),
            "left": random.randint(5, 85),
            "top": random.randint(10, 280),
            "rotate": random.randint(-20, 20),
            "weight": "bold" if count > max_count * 0.5 else "normal",
        })
    return cloud


# ============================================================
# 主分析 (从 analyze_chat.py 改写, 接受已加载的 data)
# ============================================================

def analyze(data: dict, output_stats: Path, output_text: Path) -> dict:
    messages = data.get("messages", [])
    if not messages:
        raise RuntimeError("当天没有消息, 没法做日报")

    sender_avatar_map, name_avatar_map = build_avatar_maps(data, messages)

    total_messages = len(messages)
    text_messages = [m for m in messages if m.get('type') in (0, 2)]

    active_users = set(get_display_name(m) for m in messages)
    timestamps = [m['timestamp'] for m in messages]
    start_time, start_dt = fmt_ts(min(timestamps))
    end_time, end_dt = fmt_ts(max(timestamps))
    date_str = start_dt.strftime('%Y-%m-%d')

    # === Top Talkers ===
    user_msg_counts = Counter(get_display_name(m) for m in messages)
    top_talkers_tuple = user_msg_counts.most_common(3)
    name_sender_counter = defaultdict(Counter)
    for m in messages:
        sender = m.get("sender")
        if sender:
            name_sender_counter[get_display_name(m)][sender] += 1
    top_talkers = []
    top_talker_names = set()
    for rank, (name, count) in enumerate(top_talkers_tuple, 1):
        top_talkers.append({"rank": rank, "name": name, "count": count})
        top_talker_names.add(name)

    talker_all_text = defaultdict(list)
    for m in text_messages:
        n = get_display_name(m)
        if n in top_talker_names:
            talker_all_text[n].append(m.get('content', '') or '')
    for talker in top_talkers:
        n = talker["name"]
        common_words = []
        if JIEBA_AVAILABLE and n in talker_all_text:
            combined = " ".join(talker_all_text[n])
            words = [w for w in jieba.cut(combined) if len(w) > 1 and w not in TALKER_STOPWORDS]
            common_words = [w for w, _ in Counter(words).most_common(5)]
        talker["common_words"] = common_words
        talker["avatar"] = resolve_avatar_for_name(
            n, name_sender_counter, sender_avatar_map, name_avatar_map,
        )

    # === Night Owl ===
    candidates = []
    for m in messages:
        ts = m['timestamp']
        dt = datetime.datetime.fromtimestamp(ts)
        h = dt.hour
        if NIGHT_HOURS(h):
            minutes_from_23 = (h - 23 if h >= 23 else h + 1) * 60 + dt.minute
            candidates.append({
                "name": get_display_name(m),
                "time": dt.strftime('%H:%M'),
                "lateness": minutes_from_23,
                "content": m.get('content', '') or '',
                "raw_ts": ts,
            })
    night_owl = None
    if candidates:
        candidates.sort(key=lambda x: x['lateness'], reverse=True)
        champ = candidates[0]
        count = sum(1 for c in candidates if c['name'] == champ['name'])
        night_owl = {
            "name": champ['name'],
            "last_time": champ['time'],
            "msg_count": count,
            "last_msg": champ['content'] or "[非文本消息]",
            "title": "熬夜冠军",
            "avatar": resolve_avatar_for_name(champ['name'], name_sender_counter, sender_avatar_map, name_avatar_map),
        }

    # === Word Cloud ===
    word_cloud_data = generate_word_cloud_data(text_messages)

    # === Simplified Text (5 分钟窗口压缩) ===
    TIME_WINDOW = 5 * 60
    MAX_LINES_PER_CHUNK = 1800
    MAX_LINE_LENGTH = 1600
    MAX_CONTENT_LENGTH = 200
    MAX_SEGMENT_LENGTH = 500

    groups, current, win_start = [], [], None
    for m in text_messages:
        ts = m['timestamp']
        if win_start is None:
            win_start = ts; current = [m]
        elif ts - win_start <= TIME_WINDOW:
            current.append(m)
        else:
            groups.append(current)
            current = [m]; win_start = ts
    if current:
        groups.append(current)

    chat_name = data['meta'].get('name', 'Unknown')
    summary_header = f"=== 群名称: {chat_name} | 日期: {date_str} | 消息总数: {total_messages} ==="
    lines = [summary_header]
    for g in groups:
        s_dt = datetime.datetime.fromtimestamp(g[0]['timestamp'])
        e_dt = datetime.datetime.fromtimestamp(g[-1]['timestamp'])
        time_range = s_dt.strftime('%H:%M')
        if s_dt.strftime('%H:%M') != e_dt.strftime('%H:%M'):
            time_range += f"~{e_dt.strftime('%H:%M')}"
        segments, prev = [], None
        for m in g:
            c = m.get('content', '') or ''
            if c.startswith('[语音转文字] '):
                c = c[7:]
            c = c.replace('\r','').replace('\n',' ').strip()
            if not c: continue
            if len(c) > MAX_CONTENT_LENGTH:
                c = c[:MAX_CONTENT_LENGTH] + '...'
            n = get_display_name(m)
            if n == prev and segments:
                segments[-1] += '/' + c
            else:
                segments.append(f"{n}:{c}"); prev = n
        segments = [s[:MAX_SEGMENT_LENGTH] + '...' if len(s) > MAX_SEGMENT_LENGTH else s for s in segments]
        prefix = f"[{time_range}] "
        cur = prefix
        for seg in segments:
            if cur == prefix:
                cur += seg
            elif len(cur) + 3 + len(seg) > MAX_LINE_LENGTH:
                lines.append(cur)
                cur = prefix + seg
            else:
                cur += ' | ' + seg
        if cur != prefix:
            lines.append(cur)

    chunk_paths = []
    if len(lines) <= MAX_LINES_PER_CHUNK:
        output_text.write_text("\n".join(lines), encoding='utf-8')
        chunk_paths.append(str(output_text))
    else:
        base, ext = os.path.splitext(str(output_text))
        idx = 1
        total = (len(lines) + MAX_LINES_PER_CHUNK - 1) // MAX_LINES_PER_CHUNK
        for i in range(0, len(lines), MAX_LINES_PER_CHUNK):
            cl = lines[i:i+MAX_LINES_PER_CHUNK]
            if i > 0:
                cl.insert(0, f"{summary_header} (第{idx}/{total}部分)")
            p = f"{base}_{idx}{ext}"
            Path(p).write_text("\n".join(cl), encoding='utf-8')
            chunk_paths.append(p)
            idx += 1

    stats = {
        "meta": {
            "name": chat_name,
            "source_chat_path": None,
            "source_chatroom": data['meta'].get('groupId'),
            "date": date_str,
            "total_count": total_messages,
            "active_user_count": len(active_users),
            "time_range": f"{start_dt.strftime('%H:%M')} 至 {end_dt.strftime('%H:%M')}",
        },
        "top_talkers": top_talkers,
        "night_owl": night_owl,
        "word_cloud": word_cloud_data,
        "name_avatar_map": name_avatar_map,
        "raw_text_paths": chunk_paths,
    }
    output_stats.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    return stats


# ============================================================
# CLI
# ============================================================

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("talker", help="会话 ID, 比如 50341992009@chatroom 或 wxid_xxx")
    p.add_argument("--date", default=datetime.date.today().strftime("%Y-%m-%d"), help="目标日期 YYYY-MM-DD (默认今天)")
    p.add_argument("--workdir", default=None, help="输出目录 (默认: ~/.claude/skills/wechat-daily-report/temp_<ts>/)")
    p.add_argument("--secrets", default=None, help="WeFlow secrets.json")
    p.add_argument("--auto-sync", action="store_true", help="如果快照不够新或不存在, 自动调 sync 拉新的(默认 wx-cli, 没有回退 WeFlow)")
    p.add_argument("--source", default=None, help="直接指定一个 ChatLab JSON 文件作为输入(跳过快照查找)")
    p.add_argument("--prefer", choices=["wx", "weflow"], default="wx", help="--auto-sync 时优先走哪条路径(默认 wx-cli)")
    args = p.parse_args(argv)

    here = Path(__file__).resolve().parent.parent
    secrets_path = Path(args.secrets) if args.secrets else (here / "secrets.json")
    cache_dir = here / "cache"

    # === 找快照 ===
    if args.source:
        snap = Path(args.source)
        if not snap.exists():
            print(f"❌ source 不存在: {snap}", file=sys.stderr); return 2
    else:
        snap = find_latest_snapshot(cache_dir, args.talker)
        if not snap:
            if args.auto_sync:
                snap = auto_sync(args.talker, secrets_path, date=args.date, prefer=args.prefer)
            else:
                print(f"❌ 没找到 {args.talker} 的快照, 加 --auto-sync 自动拉", file=sys.stderr)
                return 2
    print(f"📂 用快照: {snap}")

    data = json.loads(snap.read_text(encoding='utf-8'))

    # === 过滤当天 ===
    filtered = filter_by_date(data, args.date)
    n = len(filtered['messages'])
    if n == 0:
        print(f"⚠️  快照里 {args.date} 这天没有消息. 可以加 --auto-sync 拉最新的, 或换日期")
        return 3
    print(f"📊 {args.date} 当天 {n} 条消息")

    # === 工作目录 ===
    if args.workdir:
        wd = Path(args.workdir)
    else:
        ts = int(datetime.datetime.now().timestamp())
        wd = Path(os.path.expanduser(f"~/.claude/skills/wechat-daily-report/temp_{ts}"))
    wd.mkdir(parents=True, exist_ok=True)
    print(f"💼 工作目录: {wd}")

    # === 分析 ===
    stats = analyze(
        filtered,
        output_stats=wd / "stats.json",
        output_text=wd / "simplified_chat.txt",
    )

    # === 元信息 (帮 generate_report 步骤定位 vault 输出位置) ===
    meta_extra = {
        "talker": args.talker,
        "chat_name": filtered['meta'].get('name'),
        "chat_type": "group" if "@chatroom" in args.talker else "private",
        "date": args.date,
        "vault_root": str(Path.cwd()),
        "snapshot_path": str(snap),
    }
    (wd / "meta.json").write_text(json.dumps(meta_extra, ensure_ascii=False, indent=2), encoding='utf-8')

    print("")
    print("✅ 第一阶段完成 (stats + simplified_chat)")
    print("")
    print(f"📁 工作目录: {wd}")
    print(f"   ├─ stats.json")
    print(f"   ├─ simplified_chat.txt  ({len(stats['raw_text_paths'])} 个文件)")
    print(f"   └─ meta.json")
    print("")
    print("下一步 (Claude 必须做):")
    print(f"  1. 读 ~/.claude/skills/wechat-daily-report/references/ai_prompt.md")
    print(f"  2. 读 {wd}/stats.json 和 simplified_chat*.txt")
    print(f"  3. 生成 ai_content.json 写到 {wd}/ai_content.json")
    print(f"  4. 跑 generate_report.py:")
    print(f"     cd ~/.claude/skills/wechat-daily-report")
    print(f"     source .venv/bin/activate")
    print(f"     python scripts/generate_report.py \\")
    print(f"       --stats {wd}/stats.json \\")
    print(f"       --ai-content {wd}/ai_content.json \\")
    print(f"       --output {wd}/report.png \\")
    print(f"       --clean-temp")
    print("")
    print(f"📊 TOP 3 话痨参考:")
    for t in stats['top_talkers']:
        cw = "/".join(t.get('common_words', [])) or '-'
        print(f"   {t['rank']}. {t['name']:<20} {t['count']} 条  常用词: {cw}")
    if stats['night_owl']:
        no = stats['night_owl']
        print(f"🌙 熬夜冠军: {no['name']} (最晚 {no['last_time']}, 共 {no['msg_count']} 条)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
