"""给 ltm 建向量：事件摘要 + 冷层原话，写进 idx.db 的 vec 表。

用法：
    export LTM_EMBED_KEY=sk-xxx
    export LTM_EMBED_API=https://api.siliconflow.cn/v1/embeddings   # 可选
    export LTM_EMBED_MODEL=BAAI/bge-m3                              # 可选
    python3 build_vec.py --root ~/.ltm

要点：
  - 幂等：已存在的 id 跳过，可反复跑
  - 断点续跑：每 BATCH*COMMIT 条提交一次
  - 失败不致命：算不出的那条留空，检索时跳过它，词法照常
"""
import argparse
import glob
import json
import os
import sqlite3
import struct
import sys
import time
import urllib.request

DEFAULT_API = os.environ.get("LTM_EMBED_API", "https://api.siliconflow.cn/v1/embeddings")
DEFAULT_MODEL = os.environ.get("LTM_EMBED_MODEL", "BAAI/bge-m3")
BATCH = 16
COMMIT_EVERY = 200


def get_key():
    key = os.environ.get("LTM_EMBED_KEY", "").strip()
    if key:
        return key
    path = os.environ.get("LTM_EMBED_KEY_FILE", "")
    if path and os.path.exists(path):
        return open(path, encoding="utf-8").read().strip()
    return ""


def embed_batch(texts, api, model, key, timeout=120):
    body = json.dumps({"model": model, "input": texts, "encoding_format": "float"}).encode()
    req = urllib.request.Request(api, data=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return [x["embedding"] for x in d["data"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("LTM_ROOT", os.path.expanduser("~/.ltm")))
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0, help="只建前 N 条（试水用）")
    a = ap.parse_args()

    key = get_key()
    if not key:
        sys.exit("缺 LTM_EMBED_KEY（或 LTM_EMBED_KEY_FILE）")

    db = os.path.join(a.root, "idx.db")
    if not os.path.exists(db):
        sys.exit("找不到 %s，先让 ltm2.py 建库" % db)

    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS vec(
        id TEXT PRIMARY KEY, kind TEXT, ts REAL, dim INTEGER, v BLOB)""")
    have = {r[0] for r in con.execute("SELECT id FROM vec")}
    print("已有向量", len(have))

    items = []
    ev = os.path.join(a.root, "events.jsonl")
    if os.path.exists(ev):
        for ln in open(ev, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            r = json.loads(ln)
            s = r.get("summary") or ""
            if s and r["id"] not in have:
                items.append((r["id"], "event", float(r.get("ts") or 0), s[:800]))
    for f in sorted(glob.glob(os.path.join(a.root, "raw", "*.jsonl"))):
        for ln in open(f, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            c = (r.get("content") or "").strip()
            if c and r["id"] not in have:
                items.append((r["id"], "cold", float(r.get("ts") or 0), c[:800]))

    if a.limit:
        items = items[:a.limit]
    print("待建", len(items), "条")
    if not items:
        return

    t0 = time.time()
    ok = fail = 0
    pending = []
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        try:
            vecs = embed_batch([c[3] for c in chunk], a.api, a.model, key)
        except Exception as e:
            print("  ✗ 批次 %d 失败 %s" % (i, str(e)[:80]))
            fail += len(chunk)
            time.sleep(2)
            continue
        for (mid, kind, ts, _), v in zip(chunk, vecs):
            blob = struct.pack("%df" % len(v), *v)
            pending.append((mid, kind, ts, len(v), blob))
            ok += 1
        if len(pending) >= COMMIT_EVERY:
            con.executemany("INSERT OR REPLACE INTO vec VALUES (?,?,?,?,?)", pending)
            con.commit()
            pending = []
            el = max(time.time() - t0, 1e-6)
            print("  [%d/%d] %.0fs  %.1f 条/s" % (i + len(chunk), len(items), el, ok / el),
                  flush=True)
    if pending:
        con.executemany("INSERT OR REPLACE INTO vec VALUES (?,?,?,?,?)", pending)
        con.commit()
    print("完成: ok=%d fail=%d 用时 %.0fs" % (ok, fail, time.time() - t0))
    print("库内向量:", con.execute("SELECT count(*) FROM vec").fetchone()[0])
    con.close()


if __name__ == "__main__":
    main()
