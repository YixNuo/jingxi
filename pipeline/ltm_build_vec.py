"""给 ltm2 建向量：事件摘要 + 冷层原话，写进 idx.db 的 vec 表。

- 模型：BAAI/bge-m3（1024 维，中文强，便宜）
- 幂等：已存在的 id 跳过
- 断点续跑：每 200 条提交一次
- 向量只是加速器：算不出/失败的留空，检索时跳过那一路，不影响词法
"""
import json, os, sqlite3, time, urllib.request, glob, struct, sys

KEY = (os.environ.get("LTM_EMBED_KEY", "").strip()
       or (open(os.environ["LTM_EMBED_KEY_FILE"], encoding="utf-8").read().strip()
           if os.environ.get("LTM_EMBED_KEY_FILE") else ""))
os.environ["no_proxy"] = "*"
BASE = "https://api.siliconflow.cn/v1/embeddings"
MODEL = "BAAI/bge-m3"
ROOT = os.environ.get("LTM_ROOT", os.path.expanduser("~/.ltm"))
DB = os.path.join(ROOT, "idx.db")
BATCH = 16          # 一次发多少条
COMMIT_EVERY = 200


def embed_batch(texts):
    body = json.dumps({"model": MODEL, "input": texts, "encoding_format": "float"}).encode()
    req = urllib.request.Request(BASE, data=body, headers={
        "Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return [x["embedding"] for x in d["data"]]


def main():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS vec(
        id TEXT PRIMARY KEY, kind TEXT, ts REAL, dim INTEGER, v BLOB)""")
    have = {r[0] for r in con.execute("SELECT id FROM vec")}
    print("已有向量", len(have))

    items = []
    # 1) 事件摘要
    for ln in open(os.path.join(ROOT, "events.jsonl"), encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        r = json.loads(ln)
        s = r.get("summary") or ""
        if s and r["id"] not in have:
            items.append((r["id"], "event", float(r.get("ts") or 0), s[:800]))
    # 2) 冷层原话
    for f in sorted(glob.glob(os.path.join(ROOT, "raw", "*.jsonl"))):
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

    print("待建", len(items), "条")
    if not items:
        return

    t0 = time.time()
    ok = fail = 0
    pending = []
    for i in range(0, len(items), BATCH):
        chunk = items[i:i + BATCH]
        texts = [c[3] for c in chunk]
        try:
            vecs = embed_batch(texts)
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
            el = time.time() - t0
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
