#!/usr/bin/env python3
"""publish_daily.py — 把指定日期所有群的报告聚合成一个网页, push 到 GitHub Pages.

流程:
  1. 扫 vault/聊天记录导出/群聊/*/reports/<date>-日报.{ai-content,stats}.json
  2. 渲染 daily_combined.html.j2 → /tmp/qun-riba-pages/<YYYYMMDD>/index.html
  3. 重新生成 /tmp/qun-riba-pages/index.html (日历)
  4. git commit + push

用法:
  publish_daily.py --date YYYY-MM-DD [--push] [--no-skipped-shown]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path


WEEKDAYS_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def slug(text: str) -> str:
    """生成 anchor id"""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[^\w一-鿿]+", "-", text)
    return text.strip("-").lower()[:40] or "group"


def load_json_safe(p: Path) -> dict | None:
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠️  读 {p} 失败: {e}", file=sys.stderr)
        return None


def collect_groups_for_date(vault: Path, date: str, targets_file: Path | None = None) -> list[dict]:
    """
    扫描 vault/聊天记录导出/群聊/<群名>/reports/<date>-日报.{ai-content,stats}.json
    如果指定了 targets_file (id|name 格式), 按那个顺序排; 否则按文件名字母序
    """
    base = vault / "聊天记录导出" / "群聊"
    if not base.exists():
        return []

    # 读 TARGETS 顺序
    target_order: list[tuple[str, str]] = []
    if targets_file and targets_file.exists():
        for line in targets_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "|" in line:
                tid, tname = line.split("|", 1)
                target_order.append((tid.strip(), tname.strip()))

    # 直接从 daily_report.sh TARGETS 数组提取
    if not target_order:
        sh = vault / ".claudian" / "wechat" / "bin" / "daily_report.sh"
        if sh.exists():
            txt = sh.read_text(encoding="utf-8")
            m = re.search(r"declare -a TARGETS=\(\s*\n((?:.*\n)*?)\)", txt)
            if m:
                for ln in m.group(1).splitlines():
                    mm = re.search(r'"([^"]+)\|([^"]+)"', ln)
                    if mm:
                        target_order.append((mm.group(1), mm.group(2)))

    groups = []
    for tid, tname in target_order:
        report_dir = base / tname / "reports"
        ai_path = report_dir / f"{date}-日报.ai-content.json"
        stats_path = report_dir / f"{date}-日报.stats.json"
        ai = load_json_safe(ai_path)
        stats = load_json_safe(stats_path)

        if ai and stats:
            meta = stats.get("meta", {})
            wc = stats.get("word_cloud", []) or []
            top_thr = (wc[0]["count"] * 0.5) if wc else 999
            groups.append({
                "name": tname,
                "anchor": "g-" + slug(tname),
                "skipped": False,
                "message_count": meta.get("total_count", 0),
                "active_users": meta.get("active_user_count", 0),
                "time_range": meta.get("time_range", ""),
                "ai": ai,
                "word_cloud": wc,
                "top_word_threshold": top_thr,
            })
        else:
            # 该群当天被跳过(消息太少)或没数据
            # 看看有没有 stats 但没有 ai (说明被门槛挡了)
            mc = 0
            if stats:
                mc = stats.get("meta", {}).get("total_count", 0)
            groups.append({
                "name": tname,
                "anchor": "g-" + slug(tname),
                "skipped": True,
                "message_count": mc,
                "active_users": 0,
                "time_range": "",
                "ai": None,
                "word_cloud": [],
                "top_word_threshold": 999,
            })

    return groups


def render_combined(date: str, groups: list[dict], template_dir: Path) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True, lstrip_blocks=True,
    )
    tmpl = env.get_template("daily_combined.html.j2")

    d = dt.datetime.strptime(date, "%Y-%m-%d")
    total_msgs = sum(g["message_count"] for g in groups)
    total_users = sum(g["active_users"] for g in groups if not g["skipped"])

    return tmpl.render(
        date=date,
        weekday=WEEKDAYS_ZH[d.weekday()],
        groups=groups,
        total_messages=total_msgs,
        total_active_users=total_users,
        generated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def render_index(pages_dir: Path, template_dir: Path) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True, lstrip_blocks=True,
    )
    tmpl = env.get_template("index.html.j2")

    days = []
    for d in sorted(pages_dir.iterdir(), reverse=True):
        if not d.is_dir(): continue
        if not re.match(r"^\d{8}$", d.name): continue
        date_compact = d.name
        date = f"{date_compact[:4]}-{date_compact[4:6]}-{date_compact[6:]}"
        try:
            dd = dt.datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            continue
        # 从 index.html 里粗略提取统计 (可选)
        idx = d / "index.html"
        groups_count, msg_count = 0, 0
        if idx.exists():
            txt = idx.read_text(encoding="utf-8", errors="ignore")
            m1 = re.search(r"覆盖 <span>(\d+)</span>", txt)
            m2 = re.search(r"总消息 <span>([\d,]+)</span>", txt)
            if m1: groups_count = int(m1.group(1))
            if m2: msg_count = int(m2.group(1).replace(",", ""))
        days.append({
            "date": date,
            "date_compact": date_compact,
            "weekday": WEEKDAYS_ZH[dd.weekday()],
            "groups": groups_count,
            "messages": msg_count,
        })

    return tmpl.render(
        days=days,
        updated_at=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


def git_publish(pages_dir: Path, date: str) -> int:
    cmds = [
        ["git", "add", "-A"],
        ["git", "-c", "user.name=siuserxiaowei", "-c", "user.email=siuserxiaowei@users.noreply.github.com",
         "commit", "-m", f"daily report {date}", "--allow-empty"],
        ["git", "push", "origin", "main"],
    ]
    for c in cmds:
        rc = subprocess.run(c, cwd=pages_dir).returncode
        if rc != 0 and "commit" in c[3:]:
            print(f"  ⚠️  {' '.join(c)} 退出 {rc} (可能没东西可提交, 继续)")
        elif rc != 0:
            print(f"  ❌ {' '.join(c)} 失败")
            return rc
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--date", default=(dt.date.today() - dt.timedelta(days=1)).isoformat(), help="YYYY-MM-DD (默认昨天)")
    p.add_argument("--vault", default=None, help="vault 根目录")
    p.add_argument("--pages-dir", default="/tmp/qun-riba-pages", help="GitHub Pages 本地 clone")
    p.add_argument("--template-dir", default=None, help="Jinja2 模板目录")
    p.add_argument("--push", action="store_true", help="推到 GitHub")
    args = p.parse_args(argv)

    here = Path(__file__).resolve().parent.parent
    vault = Path(args.vault).resolve() if args.vault else (here.parent.parent.resolve())
    template_dir = Path(args.template_dir) if args.template_dir else (here / "templates")
    pages_dir = Path(args.pages_dir)
    if not pages_dir.exists() or not (pages_dir / ".git").exists():
        print(f"❌ pages_dir {pages_dir} 不是 git 仓库", file=sys.stderr); return 2

    print(f"📅 日期: {args.date}")
    print(f"📂 vault: {vault}")
    print(f"📂 pages: {pages_dir}")

    # 收集
    groups = collect_groups_for_date(vault, args.date)
    if not groups:
        print(f"❌ 没找到 {args.date} 的任何群报告", file=sys.stderr); return 1

    rendered_count = sum(1 for g in groups if not g["skipped"])
    print(f"📊 {len(groups)} 个目标 / {rendered_count} 个有完整内容 / {len(groups) - rendered_count} 个被跳过")

    # 渲染当天 + 写入
    date_compact = args.date.replace("-", "")
    day_dir = pages_dir / date_compact
    day_dir.mkdir(parents=True, exist_ok=True)
    html = render_combined(args.date, groups, template_dir)
    (day_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"✅ {day_dir}/index.html ({len(html)} bytes)")

    # 重新生成日历
    idx_html = render_index(pages_dir, template_dir)
    (pages_dir / "index.html").write_text(idx_html, encoding="utf-8")
    print(f"✅ {pages_dir}/index.html (日历)")

    # README + .nojekyll (避免 Jekyll 处理)
    (pages_dir / ".nojekyll").touch()
    readme = pages_dir / "README.md"
    if not readme.exists():
        readme.write_text(
            "# 群日报\n\nDaily reports auto-generated by [wechat-obsidian-pipeline]"
            "(https://github.com/siuserxiaowei/wechat-obsidian-pipeline).\n\n"
            "Visit https://siuserxiaowei.github.io/qun-riba/\n",
            encoding="utf-8"
        )

    if args.push:
        print(f"\n🚀 推送到 GitHub...")
        return git_publish(pages_dir, args.date)
    else:
        print(f"\n(--push 没加, 没推. 现在你可以本地 open {day_dir}/index.html 看效果)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
