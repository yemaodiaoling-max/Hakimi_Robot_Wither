"""Reproducible Chinese paraphrase recall evaluation for the real embedding model.

This is deliberately not a handful of hand-picked synonyms.  It contains 14
different topics, Chinese paraphrases with little/no literal overlap, and
unrelated negatives.  It uses the real FastEmbed model (or the running local
service) and prints deterministic metrics.

    python memory_embedding_eval.py                 # direct local FastEmbed
    python memory_embedding_eval.py --service-url http://127.0.0.1:18766
    python memory_embedding_eval.py --json

Exit code 2 means the real model/service is unavailable; exit code 1 means the
measured threshold was not met.  A fake encoder is intentionally NOT accepted
here: unit tests may use fakes, but this script must exercise a real model.
"""
from __future__ import annotations

import argparse
import json
import math
import sys

# Anchor and naturally rewritten paraphrase.  No pair is a synonym list; each
# topic is a different subject and the paraphrase changes the construction.
DATASET = [
    {"topic": "线性代数",
     "anchor": "矩阵把某些射线映回自身，只改变长度",
     "paraphrases": ["线性映射中保持方向不变的一维子空间", "变换后仍落在同一条直线上的非零向量"],
     "negative": "盆土长期积水会让植物根部坏掉"},
    {"topic": "声学",
     "anchor": "声音穿过不同介质时传播速度会改变",
     "paraphrases": ["同一段波在空气与固体中快慢不同", "介质密度影响声波的传递速率"],
     "negative": "物价普遍上涨使同样多的钱买到的东西变少"},
    {"topic": "量子计算",
     "anchor": "利用叠加与纠缠并行处理信息",
     "paraphrases": ["量子比特可同时处于多个状态并协同运算", "多个量子态互相牵连以加速某些计算"],
     "negative": "古代商路把亚欧物产和思想互相传递"},
    {"topic": "原神考据",
     "anchor": "提瓦特主线章节预告的发布页",
     "paraphrases": ["游戏官方放出的剧情篇章宣传影像", "旅行故事大章节的公开视频资料"],
     "negative": "接种使身体提前学会对抗病毒"},
    {"topic": "烘焙",
     "anchor": "面团发酵后体积膨胀再进烤箱",
     "paraphrases": ["让面糊醒发变大后加热定型", "酵母产生气体使胚体鼓起再烘熟"],
     "negative": "把复习分散到多天比一次猛背记得久"},
    {"topic": "摄影",
     "anchor": "光圈越小景深越大",
     "paraphrases": ["收缩镜头孔径能让前后都清晰", "减小进光口径可以扩大清晰范围"],
     "negative": "引力极强区域连光子都出不来"},
    {"topic": "编程",
     "anchor": "递归函数需要终止条件否则栈溢出",
     "paraphrases": ["函数调用自身时必须设边界防止内存耗尽", "自我调用的过程要有出口才不崩"],
     "negative": "同一段波在空气与固体中快慢不同"},
    {"topic": "天文",
     "anchor": "黑洞事件视界内光也无法逃逸",
     "paraphrases": ["引力极强区域连光子都出不来", "致密天体周围连光线都被困住"],
     "negative": "酵母产生气体使胚体鼓起再烘熟"},
    {"topic": "医学",
     "anchor": "疫苗通过训练免疫系统识别病原体",
     "paraphrases": ["接种使身体提前学会对抗病毒", "让防御机制记住入侵者的特征"],
     "negative": "收缩镜头孔径能让前后都清晰"},
    {"topic": "历史",
     "anchor": "丝绸之路连接东西方贸易与文化交流",
     "paraphrases": ["古代商路把亚欧物产和思想互相传递", "横跨大陆的通道促成货物与观念流动"],
     "negative": "函数调用自身时必须设边界防止内存耗尽"},
    {"topic": "园艺",
     "anchor": "浇水过多会导致根系缺氧腐烂",
     "paraphrases": ["盆土长期积水让植物根部坏掉", "水分排不出去时地下部分会窒息"],
     "negative": "以主音上方大三度构成的调式给人开阔感"},
    {"topic": "音乐",
     "anchor": "大调音阶听起来明亮稳定",
     "paraphrases": ["以主音上方大三度构成的调式给人开阔感", "这种音阶的色彩偏明朗而安定"],
     "negative": "古代商路把亚欧物产和思想互相传递"},
    {"topic": "心理学",
     "anchor": "间隔重复比集中背诵更利于长期记忆",
     "paraphrases": ["把复习分散到多天比一次猛背记得久", "拉开练习时间间隔能提高留存"],
     "negative": "矩阵把某些射线映回自身，只改变长度"},
    {"topic": "经济学",
     "anchor": "通货膨胀会降低货币购买力",
     "paraphrases": ["物价普遍上涨使同样多的钱买到的东西变少", "钱变得不值钱，能换到的商品减少"],
     "negative": "提瓦特主线章节预告的发布页"},
]


def cosine(left, right):
    return sum(a * b for a, b in zip(left, right))


def char_bigrams(text):
    clean = "".join(ch for ch in str(text) if "\u4e00" <= ch <= "\u9fff" or ch.isalnum())
    return {clean[i:i + 2] for i in range(max(0, len(clean) - 1))}


def lexical_overlap(left, right):
    a, b = char_bigrams(left), char_bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def evaluate(encoder, *, threshold=0.60):
    anchors = DATASET
    anchor_vectors = encoder.embed([item["anchor"] for item in anchors], mode="document")
    rows = []
    recall1 = recall3 = 0
    reciprocal = 0.0
    total = 0
    negative_margins = []
    overlaps = []
    for index, item in enumerate(anchors):
        for paraphrase in item["paraphrases"]:
            vector = encoder.embed([paraphrase], mode="query")[0]
            ranked = sorted(((cosine(vector, anchor_vectors[j]), j) for j in range(len(anchors))), reverse=True)
            rank = next(position for position, (_, j) in enumerate(ranked, 1) if j == index)
            total += 1
            recall1 += rank == 1
            recall3 += rank <= 3
            reciprocal += 1.0 / rank
            overlap = lexical_overlap(item["anchor"], paraphrase)
            overlaps.append(overlap)
            rows.append({"topic": item["topic"], "query": paraphrase, "rank": rank,
                         "top1_topic": anchors[ranked[0][1]]["topic"],
                         "top1_score": round(ranked[0][0], 4),
                         "lexical_bigram_overlap": round(overlap, 3)})
        negative_vector = encoder.embed([item["negative"]], mode="query")[0]
        positive = max(cosine(encoder.embed([paraphrase], mode="query")[0], anchor_vectors[index])
                       for paraphrase in item["paraphrases"])
        negative = cosine(negative_vector, anchor_vectors[index])
        negative_margins.append(positive - negative)
    metrics = {
        "topics": len(anchors),
        "paraphrase_queries": total,
        "recall_at_1": round(recall1 / total, 4),
        "recall_at_3": round(recall3 / total, 4),
        "mrr": round(reciprocal / total, 4),
        "zero_literal_overlap_pairs": sum(overlap == 0 for overlap in overlaps),
        "literal_overlap_ratio": round(sum(overlaps) / len(overlaps), 4),
        "mean_negative_margin": round(sum(negative_margins) / len(negative_margins), 4),
        "min_negative_margin": round(min(negative_margins), 4),
        "threshold_recall_at_1": threshold,
        "pass": (recall1 / total) >= threshold and min(negative_margins) > 0,
        "model": getattr(encoder, "model_id", "unknown"),
        "revision": getattr(encoder, "revision", "unknown"),
        "dim": getattr(encoder, "dim", 0),
        "rows": rows,
    }
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description="Real-model Chinese paraphrase recall evaluation")
    parser.add_argument("--service-url", default="", help="use a running local service instead of direct FastEmbed")
    parser.add_argument("--cache-dir", default="")
    parser.add_argument("--threshold", type=float, default=0.60)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.service_url:
        from hakimi.memory.memory_embedding_client import EmbeddingClient, EmbeddingUnavailable
        encoder = EmbeddingClient(args.service_url)
        if not encoder.health():
            print("embedding service is not available; start it or omit --service-url for direct mode", file=sys.stderr)
            return 2
    else:
        from hakimi.memory.memory_embedding_service import FastEmbedEncoder, ModelNotInstalled
        try:
            encoder = FastEmbedEncoder(cache_dir=args.cache_dir or None, allow_download=False)
        except ModelNotInstalled as exc:
            print(f"real model unavailable: {exc}", file=sys.stderr)
            print("This evaluation intentionally refuses to use a fake encoder.", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"real model failed to load: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
    try:
        metrics = evaluate(encoder, threshold=args.threshold)
    except Exception as exc:
        print(f"evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(f"model={metrics['model']} dim={metrics['dim']} topics={metrics['topics']} queries={metrics['paraphrase_queries']}")
        print(f"recall@1={metrics['recall_at_1']:.3f} recall@3={metrics['recall_at_3']:.3f} MRR={metrics['mrr']:.3f}")
        print(f"zero-literal-overlap pairs={metrics['zero_literal_overlap_pairs']}/{metrics['paraphrase_queries']} "
              f"mean literal overlap={metrics['literal_overlap_ratio']:.3f}")
        print(f"negative margin mean={metrics['mean_negative_margin']:.3f} min={metrics['min_negative_margin']:.3f}")
        for row in metrics["rows"]:
            print(f"  {row['topic']:<8} rank={row['rank']} top1={row['top1_topic']} "
                  f"score={row['top1_score']:.3f} overlap={row['lexical_bigram_overlap']:.2f} :: {row['query']}")
        print("PASS" if metrics["pass"] else "FAIL")
    return 0 if metrics["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
