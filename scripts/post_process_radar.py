#!/usr/bin/env python3
"""
ai-news-radar 后处理脚本
在 update_news.py 产出数据后执行：
  1. latest-24h.json → latest-24h-ai.json（语义修正）
  2. 从 latest-24h-all.json 的全量数据重新生成 daily-brief.json
  3. 从全量数据重新生成 stories-merged.json

纯标准库实现，无可选依赖。
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("post_process")


def parse_args():
    parser = argparse.ArgumentParser(description="雷达数据后处理")
    parser.add_argument(
        "--data-dir",
        required=True,
        help="数据目录路径，包含 latest-24h.json、latest-24h-all.json 等",
    )
    return parser.parse_args()


# ── 工具函数 ──────────────────────────────────────────────────────────

def normalize_title(title: str) -> str:
    """去空格/标点/大小写，用于去重和相似度判断。"""
    if not title:
        return ""
    import re
    t = title.lower().strip()
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)  # 只保留字母数字汉字
    return t


def title_word_set(title: str) -> set:
    """将标题分词为字词集合，用于 Jaccard 相似度。"""
    import re
    if not title:
        return set()
    t = title.lower().strip()
    # 拆分为英文词和汉字
    tokens = re.findall(r"[a-z]+|[\u4e00-\u9fff]", t)
    return set(tokens)


def jaccard_similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def title_ngrams(title: str, n: int = 2) -> set:
    """将标题拆为字符 n-gram 集合。"""
    if not title:
        return set()
    t = title.lower().strip()
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
    if len(t) < n:
        return {t}
    return {t[i:i+n] for i in range(len(t)-n+1)}


def title_word_set(title: str) -> set:
    """将标题分词为单词/字集合，用于故事线合并。
    英文按空格和标点划分单词，中文每个汉字是一个独立的词。
    比字符 n-gram 更精确，不容易将无关标题误判为相似。
    """
    if not title:
        return set()
    t = title.lower().strip()
    # 分离中文和非中文：中文按字切，英文按词切
    # 先将中文字符两侧插入空格，再按空白分词
    t = re.sub(r'([\u4e00-\u9fff])', r' \1 ', t)
    words = t.split()
    # 过滤掉纯标点符号的词
    words = [w for w in words if re.search(r'[\w\u4e00-\u9fff]', w)]
    return set(words)


def get_now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}Z"


# ── 步骤 ──────────────────────────────────────────────────────────────

def step1_rename_latest_24h(data_dir: str):
    """latest-24h.json → latest-24h-ai.json"""
    src = os.path.join(data_dir, "latest-24h.json")
    dst = os.path.join(data_dir, "latest-24h-ai.json")
    if not os.path.exists(src):
        logger.info("Skipped: latest-24h.json not found")
        return
    if os.path.exists(dst):
        logger.warning("Destination already exists, removing: %s", dst)
        os.remove(dst)
    os.rename(src, dst)
    logger.info("Renamed: latest-24h.json → latest-24h-ai.json")


def load_all_items(data_dir: str) -> list:
    """加载 latest-24h-all.json 的 items_all 字段。"""
    path = os.path.join(data_dir, "latest-24h-all.json")
    if not os.path.exists(path):
        logger.error("File not found: %s", path)
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load %s: %s", path, e)
        sys.exit(1)

    items = data.get("items_all")
    if items is None:
        logger.error("Missing 'items_all' field in %s", path)
        sys.exit(1)
    if not isinstance(items, list) or len(items) == 0:
        logger.error("'items_all' is empty or not a list in %s", path)
        sys.exit(1)

    logger.info("Loaded %d items from items_all", len(items))
    return items


def step3_generate_daily_brief(items: list, data_dir: str):
    """
    从全量条目精选最多20条生成 daily-brief.json。
    策略：
      - AI 条目优先，但最多选10条（保持AI覆盖的同时留空间给通用新闻）
      - 非 AI 条目填充剩余名额
      - 按 source_tier_rank 排序（越小越权威）
      - 同一来源最多3条
      - 标题去重
    """
    max_items = 20
    max_ai_items = 10  # AI 条目上限，保证非 AI 也有展示机会
    now_iso = get_now_iso()

    ai_items = [i for i in items if i.get("ai_is_related")]
    non_ai_items = [i for i in items if not i.get("ai_is_related")]

    logger.info("AI items: %d, non-AI items: %d", len(ai_items), len(non_ai_items))

    ai_items.sort(key=lambda i: i.get("source_tier_rank", 99))
    non_ai_items.sort(key=lambda i: i.get("source_tier_rank", 99))

    selected = []
    seen_titles = set()
    seen_urls = set()
    shared_source_count = {}  # 跨 AI/非AI 调用的共享来源计数器

    def pick_from(candidates, limit=None):
        nonlocal shared_source_count
        picked = 0
        for item in candidates:
            if limit is not None and picked >= limit:
                break
            if len(selected) >= max_items:
                break
            source = item.get("source", "")
            title = item.get("title", "")
            url = item.get("url", "")
            norm = normalize_title(title)
            if not norm or norm in seen_titles:
                continue
            if url and url in seen_urls:
                continue
            if shared_source_count.get(source, 0) >= 3:
                continue
            selected.append(item)
            seen_titles.add(norm)
            if url:
                seen_urls.add(url)
            shared_source_count[source] = shared_source_count.get(source, 0) + 1
            picked += 1

    pick_from(ai_items, limit=max_ai_items)
    pick_from(non_ai_items)

    brief_items = []
    for item in selected:
        brief_items.append({
            "title": item.get("title", ""),
            "title_zh": item.get("title_zh"),
            "url": item.get("url", ""),
            "source": item.get("source", ""),
            "source_tier_rank": item.get("source_tier_rank"),
            "source_tier_label": item.get("source_tier_label"),
            "published_at": item.get("published_at", ""),
            "ai_is_related": item.get("ai_is_related", False),
            "ai_score": item.get("ai_score", 0.0),
        })

    brief = {
        "generated_at": now_iso,
        "window_hours": 24,
        "total_items": len(brief_items),
        "items": brief_items,
    }

    path = os.path.join(data_dir, "daily-brief.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)

    logger.info("Generated daily-brief.json with %d items", len(brief_items))


def step4_generate_stories(items: list, data_dir: str):
    """
    从全量条目做故事线合并。
    1. 精确 URL 去重（相同域名+路径合并）
    2. 模糊标题合并（Jaccard 相似度 > 阈值）
    3. 输出控制：最多 500 条，优先保留多源和高权威故事
    """
    now_iso = get_now_iso()
    max_stories = 500
    similarity_threshold = 0.50  # 单词级 Jaccard 阈值（比字符 n-gram 更精确）

    # 为每条计算 URL key（域名+路径）和标题词集
    from urllib.parse import urlparse

    enriched = []
    for item in items:
        url = item.get("url", "") or ""
        try:
            parsed = urlparse(url)
            url_key = (parsed.netloc + parsed.path).rstrip("/")
        except Exception:
            url_key = url
        # 取最佳可用标题
        best_title = item.get("title_zh") or item.get("title_bilingual") or item.get("title", "")
        enriched.append({
            "item": item,
            "url_key": url_key,
            "title": best_title,
            "word_set": title_word_set(best_title),
        })

    # Phase 1: 按 url_key 精确分组
    url_groups = {}
    for e in enriched:
        url_groups.setdefault(e["url_key"], []).append(e)

    # 过滤短标题条目（纯链接类没有合并价值），只对有意义标题做模糊合并
    def has_meaningful_title(e):
        t = e["title"]
        return len(t) > 10 and not t.startswith("http")

    # Phase 2: 对标题有意义的组按标题相似度模糊合并
    url_group_list = list(url_groups.values())
    story_groups = []
    used_indices = set()

    for i, group in enumerate(url_group_list):
        if i in used_indices:
            continue
        used_indices.add(i)
        current_group = list(group)
        current_word_set = set()
        for e in current_group:
            if has_meaningful_title(e):
                current_word_set |= e["word_set"]

        for j in range(i + 1, len(url_group_list)):
            if j in used_indices:
                continue
            other = url_group_list[j]
            # 如果两个组都只有无意义标题，跳过模糊合并
            other_word_set = set()
            for e in other:
                if has_meaningful_title(e):
                    other_word_set |= e["word_set"]
            if not current_word_set or not other_word_set:
                continue
            if jaccard_similarity(current_word_set, other_word_set) >= similarity_threshold:
                current_group.extend(other)
                current_word_set |= other_word_set
                used_indices.add(j)

        story_groups.append(current_group)

    def is_meaningful_title(t):
        """过滤纯链接和极短标题。"""
        if not t:
            return False
        if len(t) < 8:
            return False
        if t.startswith("http://") or t.startswith("https://"):
            return False
        return True

    # 构建 stories 输出
    stories_out = []
    for group in story_groups:
        if not group:
            continue
        sources_set = set()
        source_names = []
        all_items_data = []
        best_rank = float("inf")
        best_title = ""
        best_url = ""

        for e in group:
            item = e["item"]
            src = item.get("source", "")
            if src and src not in sources_set:
                sources_set.add(src)
                source_names.append(src)
            item_title = e.get("title", "") or item.get("title", "")
            all_items_data.append(item)
            rank = item.get("source_tier_rank", 99)
            # 选权威度最高且标题有意义的条目作为代表
            if rank < best_rank or (rank == best_rank and len(item_title) > len(best_title)):
                best_rank = rank
                best_title = item_title
                best_url = item.get("url", "")

        # 跳过无意义标题的故事（纯链接/极短）
        if not is_meaningful_title(best_title):
            continue

        # 复合评分：权威度优先，多源加分，纯单源热议故事降权
        source_count = len(source_names)
        if source_count >= 2:
            # 多源故事：整体加分
            score = (100 - best_rank) * 100 + source_count * 10 + 50
        elif best_rank <= 3:
            # 高权威单源
            score = (100 - best_rank) * 100 + 20
        else:
            # 热议话题单源（rank=5）：降权，但不完全排除
            score = 5 + (source_count * 2)

        story = {
            "story_id": f"story_{hashlib.md5(best_url.encode()).hexdigest()[:8]}",
            "title": best_title,
            "url": best_url,
            "primary_url": best_url,
            "source_count": len(source_names),
            "source_names": source_names,
            "sources": [
                {
                    "id": item.get("id", ""),
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", ""),
                    "site_id": item.get("site_id", ""),
                    "published_at": item.get("published_at", ""),
                }
                for item in all_items_data
            ],
            "item_count": len(all_items_data),
            "score": score,
        }
        stories_out.append(story)

    # 按 score 降序
    stories_out.sort(key=lambda s: s["score"], reverse=True)

    # 裁剪到合理数量：保留高评分（多源+高权威），再补充 top 热议单源
    kept = []
    # 第一轮：所有多源故事 + 高权威单源（rank <= 3）
    for s in stories_out:
        if len(kept) >= max_stories:
            break
        if s["source_count"] >= 2 or s["score"] >= 50:
            kept.append(s)
    # 第二轮：如果还没满，补充热议单源（rank=5, 评分7）
    if len(kept) < max_stories:
        for s in stories_out:
            if len(kept) >= max_stories:
                break
            if s not in kept:
                kept.append(s)
    stories_out = kept

    result = {
        "generated_at": now_iso,
        "window_hours": 24,
        "total_stories": len(stories_out),
        "stories": stories_out,
    }

    path = os.path.join(data_dir, "stories-merged.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    logger.info("Generated stories-merged.json with %d stories", len(stories_out))


# ── 主流程 ─────────────────────────────────────────────────────────────

def main():
    args = parse_args()
    data_dir = os.path.abspath(args.data_dir)

    if not os.path.isdir(data_dir):
        logger.error("Data directory does not exist: %s", data_dir)
        sys.exit(1)

    logger.info("Post-processing started: data_dir=%s", data_dir)

    # Step 1: 重命名
    step1_rename_latest_24h(data_dir)

    # Step 2: 加载全量数据
    items = load_all_items(data_dir)

    # Step 3: 生成 daily-brief
    step3_generate_daily_brief(items, data_dir)

    # Step 4: 生成 stories-merged
    step4_generate_stories(items, data_dir)

    logger.info("Post-processing completed successfully")


if __name__ == "__main__":
    main()
