#!/usr/bin/env python3
"""render_simonlin.py — 用 Simon Lin 群日报风格生成 PNG.

输入: stats.json (来自 wf_report.py) + ai_content.json (Simon schema)
输出: HTML + PNG

用法:
  render_simonlin.py --stats stats.json --ai-content ai_content.json
                     --output report.png
                     [--template simonlin.html.j2]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path


VIEWPORT_WIDTH = 430
VIEWPORT_HEIGHT = 932
DEVICE_SCALE_FACTOR = 3


def load_json(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stats", required=True)
    p.add_argument("--ai-content", required=True)
    p.add_argument("--output", default="report.png")
    p.add_argument("--template", default=None, help="自定义模板路径")
    p.add_argument("--source", default="wx-cli", help="footer 显示的数据源标签")
    args = p.parse_args(argv)

    here = Path(__file__).resolve().parent.parent
    template_path = Path(args.template) if args.template else (here / "templates" / "simonlin.html.j2")
    if not template_path.exists():
        print(f"❌ 模板不存在: {template_path}", file=sys.stderr)
        return 2

    stats = load_json(Path(args.stats))
    ai = load_json(Path(args.ai_content))

    meta = stats.get("meta", {})
    word_cloud = stats.get("word_cloud", []) or []
    top_word_threshold = (word_cloud[0]["count"] * 0.5) if word_cloud else 999

    ctx = {
        "chat_name": meta.get("name", "Unknown"),
        "date": meta.get("date", ""),
        "total_count": meta.get("total_count", 0),
        "active_users": meta.get("active_user_count", 0),
        "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": args.source,
        "header_emoji": ai.get("header_emoji", "🐉"),
        "quick_quotes": ai.get("quick_quotes", []),
        "ranking_top3": ai.get("ranking_top3", []),
        "resources": ai.get("resources", []),
        "story_sections": ai.get("story_sections", []),
        "echo_quote": ai.get("echo_quote"),
        "word_cloud": word_cloud,
        "top_word_threshold": top_word_threshold,
        "daily_awards": ai.get("daily_awards", []),
    }

    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError:
        print("❌ 缺 jinja2: pip install jinja2", file=sys.stderr); return 2

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template(template_path.name)
    html = template.render(**ctx)

    out_png = Path(args.output)
    out_html = out_png.with_suffix(".html")
    out_html.write_text(html, encoding="utf-8")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 缺 playwright: pip install playwright && playwright install chromium", file=sys.stderr)
        return 2

    print(f"📸 渲染 PNG: {out_png}")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx_obj = browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            device_scale_factor=DEVICE_SCALE_FACTOR,
        )
        page = ctx_obj.new_page()
        page.goto(f"file://{out_html.resolve()}", wait_until="networkidle")
        page.wait_for_load_state("domcontentloaded")
        page.locator("body").screenshot(path=str(out_png))
        browser.close()

    print(f"✅ {out_png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
