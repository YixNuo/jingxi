#!/usr/bin/env python3
"""切段：把新写入冷层的对话切成"事件段"。

一段 = 一件事。切点看三样：
  · 时间空档超过 GAP_MINUTES
  · 用户消息以"重新起头"的词开头（对了/话说/还有…）
  · 单段字符数超过 HARD_MAX_CHARS

幂等：已出现在某段里的冷层 id 不再重复使用。
用法：
    python3 ltm_segment.py            # 增量
    python3 ltm_segment.py --since 0  # 全量重建（慎用，会再切一遍）
"""
import argparse
import datetime
import glob
import json
import os

ROOT = os.environ.get("LTM_ROOT", os.path.expanduser("~/.ltm"))
SEGS = os.environ.get("LTM_SEGS", os.path.join(ROOT, "segments.jsonl"))
GAP_MINUTES = 40
HARD_MAX_CHARS = 12000
OPENERS = ("对了", "话说", "还有", "另外", "那个", "诶", "哎", "哦对", "突然想起",
           "我想说", "说个事", "我说", "话说回来", "欸")
SHORT_MOVES = ("睡了", "我先", "等会", "回来", "在吗", "先这样", "稍等")


def ensure_files():
    os.makedirs(os.path.dirname(SEGS) or ".", exist_ok=True)
    if not os.path.exists(SEGS):
        open(SEGS, "a", encoding="utf-8").close()


def load_segments():
    ensure_files()
    out = []
    for ln in open(SEGS, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def load_new_records(since, used_ids):
    rows = []
    for f in sorted(glob.glob(os.path.join(ROOT, "raw", "*.jsonl"))):
        for ln in open(f, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if float(r.get("ts") or 0) <= since:
                continue
            if r.get("id") in used_ids:
                continue
            rows.append(r)
    rows.sort(key=lambda x: float(x.get("ts") or 0))
    return rows


def cut(rows):
    """按时间空档 + 起头词 + 长度上限切段。"""
    segs = []
    cur = []
    chars = 0
    prev_ts = None

    def flush():
        nonlocal cur, chars
        if not cur:
            return
        text = "\n".join("%s: %s" % (r.get("role"), (r.get("content") or "")[:500])
                         for r in cur)
        segs.append({
            "source_ids": [r["id"] for r in cur],
            "title": ((cur[0].get("content") or "").strip().replace("\n", " "))[:40],
            "chars": len(text),
            "start": float(cur[0].get("ts") or 0),
            "end": float(cur[-1].get("ts") or 0),
        })
        cur = []
        chars = 0

    for r in rows:
        ts = float(r.get("ts") or 0)
        text = r.get("content") or ""
        should_cut = False
        if prev_ts is not None and (ts - prev_ts) / 60.0 > GAP_MINUTES:
            should_cut = True
        if r.get("role") == "user" and cur:
            head = text.strip()
            if any(head.startswith(o) for o in OPENERS):
                should_cut = True
            elif len(head) <= 8 and any(head.startswith(s) for s in SHORT_MOVES):
                should_cut = True
        if chars + len(text) > HARD_MAX_CHARS and cur:
            should_cut = True
        if should_cut:
            flush()
        cur.append(r)
        chars += len(text)
        prev_ts = ts
    flush()
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=float, default=None,
                    help="起始时间戳；缺省=已切过的最新段末尾")
    a = ap.parse_args()

    segs = load_segments()
    max_seq = max((s.get("seq", 0) for s in segs), default=0)
    used_ids = set()
    for s in segs:
        used_ids.update(s.get("source_ids") or [])

    if a.since is None:
        since = max((float(s.get("end") or 0) for s in segs), default=0.0)
    else:
        since = a.since
        if since <= 0:
            segs, used_ids, max_seq = [], set(), 0

    rows = load_new_records(since, used_ids)
    print("已有段 %d / 最大 seq %d / 新记录 %d" % (len(segs), max_seq, len(rows)))
    if not rows:
        print("没有新内容需要切段")
        return

    new_segs = cut(rows)
    seq = max_seq
    with open(SEGS, "a", encoding="utf-8") as f:
        for s in new_segs:
            seq += 1
            s["seq"] = seq
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    sizes = sorted(s["chars"] for s in new_segs)
    print("新切出 %d 段，seq %d ~ %d" % (len(new_segs), max_seq + 1, seq))
    if sizes:
        print("字数 最小 %d / 中位 %d / 最大 %d" % (sizes[0], sizes[len(sizes) // 2], sizes[-1]))


if __name__ == "__main__":
    main()
