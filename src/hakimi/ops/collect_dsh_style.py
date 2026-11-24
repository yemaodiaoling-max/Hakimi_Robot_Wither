"""Export existing Gemini outbox text as DSH-only reference data, never invent dialogue."""
from hakimi import paths
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE = paths.root()


def collect(base=BASE):
    groups = defaultdict(list)
    seen = set()
    for lane in (Path("."), Path("official_test_runtime")):
        source = lane / "state/outbox.jsonl"
        if not (base / source).is_file():
            continue
        for line_no, raw in enumerate((base / source).read_bytes().splitlines(), 1):
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if (not isinstance(row, dict) or row.get("provider") != "gemini"
                    or not re.fullmatch(r"group:[1-9][0-9]{0,19}", str(row.get("session_key", "")))):
                continue
            parts = row.get("segments", [])
            if not isinstance(parts, list) or any(not isinstance(part, str) for part in parts):
                continue
            # Only remove transport markup; retain every remaining word verbatim.
            text = [re.sub(r"\[CQ:[^\]]*\]", "", part).strip() for part in parts]
            text = [part for part in text if part]
            joined = " ".join(text)
            if not 5 <= len(joined) <= 500:
                continue
            # OOC/backend, hostile exchanges and tool/path claims are poor style references.
            if re.search(r"dsh|deepseek|gemini|谷歌|底模|后端|额度|模型|防互刷|没收到|封号|猪鼻|傻|弱智|智障|滚|废物|无能狂怒|抽陀螺|打黑工|使唤|晦气|皮痒|欠揍|蛋|萝莉|罐头|讨饭|肥鱼|白饭|翻墙|https?://|[A-Z]:[\\/]|```", joined, re.I):
                continue
            if re.search(r"急了|急眼|打脸|破大防|阴暗|抽.*陀螺|时钟|低血糖|哪来的命座|半个字|少见多怪|又想|天天全勤|一只打工|一点半|后天开学|TPU|找虐|受虐|黑历史|字都敲|赛博精神病|满意没|除了装可怜|抓你|违规图|涩图|3岁|菜就多练|才不道歉|谁让你自己|少把猫当人|听不懂怪谁|嘴欠|骂哭|喷哭|不想理你|机房不断电|谁管拔电源|路人谁理|大孝子|算力|查户口|魔怔|怼你|当空气|装死|猝死", joined):
                continue
            identity = (row["session_key"], joined)
            if identity in seen:
                continue
            created = row.get("created_at", row.get("timestamp"))
            if type(created) not in (float, int) or not math.isfinite(created):
                continue
            try:
                created_at_utc = datetime.fromtimestamp(created, timezone.utc).isoformat(timespec="seconds")
            except (ValueError, OSError, OverflowError):
                continue
            reply_id = row.get("reply_id")
            if not isinstance(reply_id, str) or not reply_id:
                continue
            seen.add(identity)
            groups[row["session_key"]].append({
                "id": reply_id, "provenance": "recorded_gemini_output", "provider": "gemini",
                "session_key": row["session_key"], "text_segments": text,
                "created_at_utc": created_at_utc,
                "source": {"file": source.as_posix(), "line": line_no,
                           "raw_sha256": hashlib.sha256(raw).hexdigest()},
            })
    selected = []
    for session, rows in sorted(groups.items()):
        # Spread the references across the run; avoid one repeated catchphrase dominating.
        endings = Counter()
        chosen = []
        ranked = sorted(rows, key=lambda row: hashlib.sha256(row["id"].encode()).hexdigest())
        for row in ranked:
            ending = row["text_segments"][-1][-1:]
            if ending in "喵哼草啧噗" and endings[ending] >= 4:
                continue
            chosen.append(row)
            endings[ending] += 1
            if len(chosen) >= 24:
                break
        selected += sorted(chosen, key=lambda row: row["created_at_utc"])
    (base / "dsh_style_examples.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected), encoding="utf-8")
    doc = ["# 仅供 dsh 的哈基米发言参考", "",
           "这些是现有运行记录中 provider=gemini 的真实生成发言，不是编写的示例，也不是模型权重训练。"
           "只摘取哈基米自己的文字，不补写群友提问；去除 CQ 引用与图片标记，正文措辞保留。"
           "来源表示生成记录，不把入队等同于平台成功送达。每项可用源文件行号与原始行 SHA-256 复核。", "",
           "只学同一群的表达节奏，不能把历史台词当作剧情、数学或现实事实的证据。"
           "旧记录有时过于嘴硬；学习群温和，剧情群先核对依据，本轮规则优先。"
           "每轮最多加载本群 4 例、共 1000 字；不额外调用模型。Gemini 的提示不加载这份数据。", "",
           "取样会去重，避开本轮刚说过的原句，并分散重复语尾；学习和考据场景优先平和措辞。"
           "已收藏表情的中文用途提示仅由文件名关键词整理，不把文件名当作已看见图片。", "",
           f"本次共 {len(selected)} 条，覆盖 {len(groups)} 个群。", ""]
    for row in selected:
        source = row["source"]
        doc += [f"## {row['session_key']} · {row['id']}", "",
                f"时间：{row['created_at_utc']}（UTC）。来源：`{source['file']}:{source['line']}`。", "",
                f"原始行 SHA-256：`{source['raw_sha256']}`", "", "```json",
                json.dumps(row["text_segments"], ensure_ascii=False), "```", ""]
    (base / "docs/dsh_style_learning.md").write_text("\n".join(doc), encoding="utf-8")
    print(json.dumps({"examples": len(selected), "groups": dict(Counter(row["session_key"] for row in selected))}, ensure_ascii=False))


if __name__ == "__main__":
    collect()
