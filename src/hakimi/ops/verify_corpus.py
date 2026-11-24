"""Check the selected dialogue against independently captured public source text.

This checks textual provenance and indexed parent/reply structure, not the human
identity of account operators. It does not fetch anything or send any messages.
"""
from hakimi import paths
import argparse
import copy
import hashlib
import json
import re
import textwrap
from collections import Counter
from pathlib import Path


BASE = paths.root()
SOURCE_HASHES = {
    "state/corpus_research/reddit_web_evidence.json": "20adc1a72ced618e1918e419279673472a142fb2b57cb9c59c9a80ff4a26b59d",
    "state/corpus_research/pchatbot_sample.txt": "7b3baf12707fb28e4ce53b7ea2969702337912205ce701c9bb4c3ab48de102e5",
    "state/corpus_research/reddit_web_evidence_expansion.json": "a36c9d452b099d95af5dcc8685252697bd8eac5f9c64120e3395d766e58fd09c",
}
PROFILES = {"tech_banter", "lore_club", "gentle_daily", "playful_acg", "quiet_default", "study_calm"}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_evidence():
    result = {}
    for relative, expected in SOURCE_HASHES.items():
        raw = (BASE / relative).read_bytes()
        require(hashlib.sha256(raw).hexdigest() == expected, f"Source capture changed: {relative}")
        result[relative] = raw.decode("utf-8")
    captures = []
    for relative in SOURCE_HASHES:
        if "reddit_web_evidence" in relative:
            captures.extend(json.loads(result[relative]))
    sections = {}
    for capture in captures:
        for section in capture.split("--------------------------------------------------------------------------------"):
            heading = section.strip().split("\n", 1)[0]
            if "https://www.reddit.com/" in heading:
                sections[sha(section)] = section
    result["reddit_sections"] = sections
    return result


def source_pairs(section):
    """Read each child with its preceding immediate ancestor in the source tree."""
    headers = list(re.finditer(r"^( *)\[([^\]\n]+)\] \[[-+]?\d+ votes?\]\s*$", section, re.M))
    parsed = []
    for i, header in enumerate(headers):
        boundary = headers[i + 1].start() if i + 1 < len(headers) else len(section)
        parsed.append({
            "depth": len(header.group(1)) // 2,
            "date": header.group(2),
            "text": textwrap.dedent(section[header.end():boundary]).strip("\r\n"),
        })
    pairs = []
    for i, child in enumerate(parsed):
        # Unlike the selection script's stack, independently scan back to the
        # first shallower comment. A skipped level is not accepted as a parent.
        for parent in reversed(parsed[:i]):
            if parent["depth"] < child["depth"]:
                if parent["depth"] + 1 == child["depth"]:
                    pairs.append((parent, child))
                break
    return pairs


def post_pairs(section):
    """Only reviewed captures that explicitly include the textual original post."""
    title = section.strip().split("\n", 1)[0]
    require(any(f"/comments/{post})" in title for post in ("mz6zqp", "vn9fxb")),
            "Original-post relationship not reviewed for this thread")
    headers = list(re.finditer(r"^( *)\[([^\]\n]+)\] \[[-+]?\d+ votes?\]\s*$", section, re.M))
    require(len(headers) > 1 and not headers[0].group(1), "Missing original post")
    def row(index):
        header = headers[index]
        end = headers[index+1].start() if index+1 < len(headers) else len(section)
        return {"depth": len(header.group(1))//2, "date": header.group(2),
                "text": textwrap.dedent(section[header.end():end]).strip("\r\n")}
    parent = row(0)
    require(parent["text"] and "SHARED URL" not in parent["text"], "Missing textual original post")
    return [(parent, row(i)) for i in range(1, len(headers)) if not headers[i].group(1)]


def verify_record(row, evidence):
    require(row.get("provenance") == "real_human_dialogue", "Not a real-source record")
    require(row.get("profile") in PROFILES, "Unknown profile")
    require(row.get("decision") == "reply", "Unobserved silence cannot be a source dialogue")
    require(not row.get("tool_context"), "Source dialogues cannot contain simulated tool results")
    require(len(row.get("context", [])) == 1, "Expected one original parent comment")
    texts = [row["context"][0]["text"], row["reply"]]
    require(all(isinstance(t, str) and t for t in texts), "Empty source text")
    source = row["source"]
    require(source.get("human_verification_limit"), "Missing authorship qualification")
    require(source.get("captured_on") == "2026-10-23", "Unexpected capture date")
    require(source.get("evidence_file") in SOURCE_HASHES, "Unapproved source capture")
    require(source.get("text_sha256") == [sha(t) for t in texts], "Text hash mismatch")

    if source.get("dataset") == "Reddit public comment excerpts":
        require("reddit_web_evidence" in source["evidence_file"], "Wrong source file")
        section = evidence["reddit_sections"].get(source.get("source_section_sha256"))
        require(section is not None, "Original source section not found")
        title = section.strip().split("\n", 1)[0]
        require(f"({source['url']})" in title, "Source URL does not match captured page")
        require(re.search(r"/comments/" + re.escape(source["post_id"]) + r"(?:/|$)", source["url"]), "Post ID mismatch")
        require(source.get("comment_ids") is None and source.get("record_id") is None,
                "The indexed source does not provide comment IDs")
        relationship = source.get("relationship")
        require(relationship in {"immediate_parent_reply_from_indexed_indentation", "original_post_to_top_level_reply_from_index"},
                "Unsupported relationship claim")
        pairs = post_pairs(section) if relationship == "original_post_to_top_level_reply_from_index" else source_pairs(section)
        matches = [(p, c) for p, c in pairs
                   if [p["text"], c["text"]] == texts
                   and [p["date"], c["date"]] == source["dates"]
                   and [p["depth"], c["depth"]] == source["depths"]]
        require(len(matches) == 1, "Exact original immediate parent/reply pair not found")
        require(source.get("license", "").startswith("No open license asserted"), "Incorrect Reddit license")
    elif source.get("dataset") == "PChatbotW author public sample":
        relative = "state/corpus_research/pchatbot_sample.txt"
        require(source["evidence_file"] == relative, "Wrong source file")
        require(source.get("source_file_sha256") == SOURCE_HASHES[relative], "Sample file hash mismatch")
        number = source["line_number"]
        require(isinstance(number, int) and number > 0, "Invalid source line")
        line = evidence[relative].splitlines()[number - 1]
        fields = line.split("\t")
        require(len(fields) == 6, "Not a published dialogue row")
        require(texts == [fields[0], fields[3]], "Text differs from original sample fields")
        require(sha(line) == source["source_line_sha256"], "Source line hash mismatch")
        require(source["record_id"] == f"data/sample.txt:line:{number}", "Line identifier mismatch")
        require(source["url"] == f"https://github.com/qhjqhj00/SIGIR2021-Pchatbot/blob/main/data/sample.txt#L{number}",
                "Incorrect author source URL")
        require(source.get("license") == "CC BY-NC 2.0", "Incorrect dataset license")
    else:
        raise ValueError("Unknown dataset")


def verify_rows(rows, evidence):
    require(len(rows) == 48, "Expected 48 reviewed source dialogues")
    require(len({r["id"] for r in rows}) == len(rows), "Duplicate record ID")
    for row in rows:
        try:
            verify_record(row, evidence)
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ValueError(f"{row.get('id', '<missing ID>')}: {exc}") from exc


def negative_checks(rows, evidence):
    altered = copy.deepcopy(rows[0])
    altered["reply"] += "喵"
    altered["source"]["text_sha256"][1] = sha(altered["reply"])
    swapped = copy.deepcopy(rows[0])
    swapped["context"][0]["text"] = rows[2]["context"][0]["text"]
    swapped["source"]["text_sha256"][0] = sha(swapped["context"][0]["text"])
    extra = []
    for predicate in [lambda row: row["source"].get("relationship") == "original_post_to_top_level_reply_from_index",
                      lambda row: row["source"].get("dataset") == "PChatbotW author public sample"]:
        candidate = next(row for row in rows if predicate(row))
        record = copy.deepcopy(candidate)
        record["reply"] += "喵"
        record["source"]["text_sha256"][1] = sha(record["reply"])
        extra.append(record)
    checks = [altered, swapped, *extra]
    for record in checks:
        try:
            verify_record(record, evidence)
        except ValueError:
            continue
        raise ValueError("Negative check failed: a modified or fabricated pair was accepted")
    return len(checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=paths.persona("style_examples.jsonl", BASE))
    parser.add_argument("--self-test", action="store_true", help="also reject a rewritten reply and a fabricated pairing")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.corpus.read_text(encoding="utf-8").splitlines() if line.strip()]
    evidence = load_evidence()
    verify_rows(rows, evidence)
    negative_count = negative_checks(rows, evidence) if args.self_test else 0
    print(json.dumps({"verified": len(rows), "profiles": dict(Counter(r["profile"] for r in rows)),
                      "datasets": dict(Counter(r["source"]["dataset"] for r in rows)),
                      "negative_checks": negative_count,
                      "scope": "Exact source text and indexed relationships; human identity not independently authenticated"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
