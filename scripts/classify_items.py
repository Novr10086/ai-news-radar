#!/usr/bin/env python3
"""
分类脚本：将 latest-24h-all-raw.json 的全量条目按类别切分为多个小 JSON，
供简报读取时各取所需，避免全量数据进上下文浪费 token。

分类维度：
  - ai          → AI 动态（ai_is_related=True + AI专属源）
  - geopolitics → 国际局势/时政（新华网、联合早报、NPR 等的非AI条目）
  - tech        → 科技商业（36氪、虎嗅、钛媒体、TechCrunch 等的非AI条目）
  - domestic    → 国内/社会（澎湃、新华社国内报道 等的非AI条目）
  - short       → 短讯/其他（NewsNow 等低优先级条目）

输出文件（均在 data_dir 下）：
  - brief-ai.json
  - brief-geopolitics.json
  - brief-tech.json
  - brief-domestic.json
  - brief-short.json
  - brief-categories.json（分类统计总表，含 classes 字段）

作者：天衍
"""

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("classify")


def parse_args():
    parser = argparse.ArgumentParser(description="雷达条目分类")
    parser.add_argument(
        "--data-dir",
        required=True,
        help="数据目录路径，需要包含 latest-24h-all-raw.json",
    )
    parser.add_argument(
        "--raw-file",
        default="latest-24h-all-raw.json",
        help="原始全量文件名（默认 latest-24h-all-raw.json）",
    )
    return parser.parse_args()


def get_now_iso():
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}Z"


# ── 分类规则（集中维护） ──

# AI 关键词（标题/源名命中即走 ai）
AI_KW = ["openai", "anthropic", "deepseek", "google gemini", "kimi",
         "claude", "gpt", "llama", "qwen", "ai ", "人工智能",
         "大模型", "机器学习", "deep learning", "machine learning",
         "hugging face", "generative ai", "生成式", "agent"]

# geopolitics 源名
GEOPOLITICS_SOURCES = ["新华网", "新华社", "联合早报", "npr", "bbc", "reuters",
                       "cnn", "the guardian", "ap news", "cctv", "央视"]

# tech 源名
TECH_SOURCES = ["36氪", "虎嗅", "钛媒体", "techcrunch", "ars technica",
                "the verge", "wired", "少数派", "info flow", "it之家",
                "hacker news", "product hunt"]

# domestic 源名
DOMESTIC_SOURCES = ["澎湃", "thepaper"]

# 新华社/新华网文章中的科技/财经关键词 → 强制定向到 tech
XINHUA_TECH_KW = ["科技", "AI", "人工智能", "数字", "量子", "芯片",
                  "港股", "股价", "回购", "上市", "财报",
                  "robot", "robotics", "startup", "融资",
                  "app", "软件", "智能", "算法", "超算"]

# geopolitics 标题关键词
GEOPOLITICS_KW = ["导弹", "制裁", "战争", "外交", "总统", "首相",
                  "欧盟", "北约", "联合国", "主权", "领土", "军事",
                  "打击", "袭击", "死亡", "疫情", "埃博拉", "多瑙河",
                  "核", "黑海", "中东", "西共体", "轮值主席",
                  "zone", "election", "strike"]

# domestic 标题关键词（社会民生）
DOMESTIC_KW = ["西瓜", "物价", "民生", "医保", "养老", "房价", "教育",
               "补贴", "国补", "工资", "消费", "就业", "招聘", "裁员",
               "流浪", "homeless"]

# tech 标题关键词
TECH_KW = ["芯片", "gpu", "tpu", "手机", "平板", "星舰",
           "spacex", "tesla", "fsd", "软件", "硬件", "开源",
           "上市", "融资", "收购", "股价", "财报", "港股",
           "字节", "阿里", "腾讯", "小米", "华为", "百度",
           "机器人", "robo", "automation", "深度学习",
           "量子", "超导", "XR", "pico", "显示", "超算",
           "app", "laptop", "ebike"]

# short 源名
SHORT_SOURCES = ["newsnow", "zeli", "buzzing"]


def classify_source(source: str, title: str, is_ai: bool) -> str:
    """
    根据来源名称和标题判断类别。
    返回: ai / geopolitics / tech / domestic / short
    """
    if not source:
        source = ""
    if not title:
        title = ""
    source_lower = source.lower()
    title_lower = title.lower()

    # ── AI 类：AI标签命中 或 AI关键词 ──
    if is_ai:
        return "ai"
    for kw in AI_KW:
        if kw in title_lower or kw in source_lower:
            return "ai"

    # ── 国际/时政源 ──
    for gs in GEOPOLITICS_SOURCES:
        if gs in source_lower:
            # 新华社/新华网里科技财经类内容截胡到 tech
            if ("新华社" in source or "新华网" in source):
                if any(kw in title_lower for kw in XINHUA_TECH_KW):
                    return "tech"
            if "npr" in source_lower and any(kw in title_lower for kw in ["tech", "ai", "robot", "startup", "gpu", "app", "review"]):
                return "tech"
            return "geopolitics"

    # ── 科技商业源 ──
    for ts in TECH_SOURCES:
        if ts in source_lower:
            return "tech"

    # ── 国内综合源 ──
    for ds in DOMESTIC_SOURCES:
        if ds in source_lower:
            return "domestic"

    # ── 标题关键词辅助分类 ──
    for kw in GEOPOLITICS_KW:
        if kw in title_lower:
            return "geopolitics"
    for kw in DOMESTIC_KW:
        if kw in title_lower:
            return "domestic"
    for kw in TECH_KW:
        if kw in title_lower:
            return "tech"

    # ── 低优先级 → short ──
    for ss in SHORT_SOURCES:
        if ss in source_lower:
            return "short"

    # ── Follow Builders(X推文) → 非AI的归入 tech ──
    if "follow builders" in source_lower:
        return "tech"

    # ── 兜底路径 ──
    # 按域名/路径线索
    if any(s in source_lower for s in ["news.cn", "thepaper"]):
        return "domestic"
    if "zaobao" in source_lower:
        return "geopolitics"
    if any(s in source_lower for s in ["36kr", "tmtpost", "huxiu", "sspai"]):
        return "tech"

    # 纯英文标题 → short
    if re.match(r"^[a-zA-Z0-9\s\-.,!?'\"()]+$", title.strip()[:50]) and len(title) > 10:
        return "short"

    return "domestic"


def main():
    args = parse_args()
    data_dir = os.path.abspath(args.data_dir)
    raw_path = os.path.join(data_dir, args.raw_file)

    if not os.path.exists(raw_path):
        logger.error("原始文件不存在: %s", raw_path)
        logger.info("回退：尝试读取 latest-24h-all.json")
        raw_path = os.path.join(data_dir, "latest-24h-all.json")
        if not os.path.exists(raw_path):
            logger.error("也无 latest-24h-all.json，退出")
            return

    with open(raw_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items_all_raw") or data.get("items_all") or data.get("items", [])
    if not items:
        logger.error("未找到条目列表")
        return

    logger.info("加载 %d 条待分类", len(items))

    classified = {"ai": [], "geopolitics": [], "tech": [], "domestic": [], "short": []}

    for item in items:
        source = item.get("source", item.get("site_name", ""))
        title = item.get("title_zh") or item.get("title_bilingual") or item.get("title", "")
        is_ai = item.get("ai_is_related", False)
        category = classify_source(source, title, is_ai)
        classified.setdefault(category, []).append(item)

    def trim(item):
        return {
            "title": item.get("title", ""),
            "title_zh": item.get("title_zh") or item.get("title", ""),
            "url": item.get("url", ""),
            "source": item.get("source", item.get("site_name", "")),
            "_site": item.get("site_name", ""),
            "tier": item.get("source_tier_rank", 5),
            "ai": item.get("ai_is_related", False),
            "ts": item.get("published_at", ""),
        }

    now_iso = get_now_iso()
    category_stats = {}

    for cat, cat_items in classified.items():
        trimmed = [trim(i) for i in cat_items]
        trimmed.sort(key=lambda i: (i["tier"], i["ts"] or ""))
        out = {
            "generated_at": now_iso,
            "category": cat,
            "total": len(trimmed),
            "items": trimmed,
        }
        out_path = os.path.join(data_dir, f"brief-{cat}.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        category_stats[cat] = len(trimmed)
        logger.info("已生成: brief-%s.json (%d 条)", cat, len(trimmed))

    summary = {
        "generated_at": now_iso,
        "total_items": len(items),
        "classes": category_stats,
        "order": ["geopolitics", "domestic", "tech", "ai", "short"],
    }
    summary_path = os.path.join(data_dir, "brief-categories.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info("已生成分类总表: brief-categories.json")

    logger.info("分类完成: %s", json.dumps(category_stats, ensure_ascii=False))


if __name__ == "__main__":
    main()
