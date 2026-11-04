#!/usr/bin/env python3
"""多模型轮换 + 并发，给事件段写摘要。

策略：
- 一次并发 N 段（默认 4）
- 每个请求轮换模型（避开被限流的渠道）
- 429/超时自动换下一个模型重试
- 断点续跑：已有 seq 跳过
"""
import json, os, sys, time, urllib.request, re, argparse, threading, queue
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ltm_scrub import scrub   # 摘要落盘前统一脱敏

API = os.environ.get("LTM_LLM_API", "https://api.openai.com/v1/chat/completions")
KEY = os.environ.get("LTM_LLM_KEY", "").strip()
ROOT = os.environ.get("LTM_ROOT", os.path.expanduser("~/.ltm"))
SEGS = os.environ.get("LTM_SEGS", os.path.join(ROOT, "segments.jsonl"))
OUT = os.environ.get("LTM_SUMS", os.path.join(ROOT, "summaries.jsonl"))

# 轮换池：优先 cheap/flash 类，避开已被限的 qodercn
MODELS = [m.strip() for m in
          os.environ.get("LTM_LLM_MODELS", "gpt-4o-mini").split(",") if m.strip()]

SYS = """你在整理一个人的长期记忆。给定一段对话，写一条短摘要。

要求：
- 60~120 字
- 写清"发生了什么、谁说的、结论是什么"
- 保留关键名词（项目名、人名、具体事物、数字）
- 不要写"用户和助手进行了交流"这种废话
- 不要评价，不要总结感受
- 直接输出摘要正文，不要任何前缀、引号、解释"""

_lock = threading.Lock()
_model_idx = [0]



def _ensure_files():
    """首次运行时 segments/summaries 还不存在，先建空文件再读。"""
    for path in (SEGS, SUMS):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                open(path, "a", encoding="utf-8").close()
        except Exception:
            pass

def next_model():
    with _lock:
        m = MODELS[_model_idx[0] % len(MODELS)]
        _model_idx[0] += 1
        return m


def load_raw_index():
    """一次性把 raw 全读进内存，避免每段扫盘。"""
    import glob
    idx = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "raw", "*.jsonl"))):
        for ln in open(f, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            idx[r["id"]] = r
    return idx


def build_text(seg, raw, max_chars=5000):
    parts = []
    total = 0
    for rid in seg["source_ids"]:
        r = raw.get(rid)
        if not r:
            continue
        c = (r.get("content") or "").strip()
        if not c:
            continue
        role = "他" if r.get("role") == "user" else "我"
        piece = f"{role}: {c[:500]}"
        parts.append(piece)
        total += len(piece)
        if total > max_chars:
            break
    return "\n".join(parts)


def call(text, model, max_tokens=400, timeout=90):
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYS},
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
    }).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "Bearer " + KEY,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    return d["choices"][0]["message"].get("content") or "", d.get("usage", {})


def work(seg, raw):
    text = build_text(seg, raw)
    if len(text) < 60:
        return seg["seq"], seg["title"], 0, "too_short"
    last = None
    for attempt in range(len(MODELS)):
        m = next_model()
        try:
            sm, usage = call(text, m)
            sm = re.sub(r"^[\s\"'“”]+|[\s\"'“”]+$", "", sm.strip())[:400]
            if len(sm) < 10:
                raise ValueError("empty summary")
            return seg["seq"], sm, usage.get("total_tokens", 0), m
        except Exception as e:
            last = f"{m}: {e}"
            time.sleep(0.5)
    return seg["seq"], None, 0, f"FAIL {last}"


def main():
    _ensure_files()
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    segs = [json.loads(l) for l in open(SEGS, encoding="utf-8") if l.strip()]
    done = set()
    if os.path.exists(OUT):
        for ln in open(OUT, encoding="utf-8"):
            try:
                done.add(json.loads(ln)["seq"])
            except Exception:
                pass
    todo = [s for s in segs if s["seq"] not in done]
    if a.limit:
        todo = todo[:a.limit]
    print("总 %d / 已做 %d / 待做 %d / 并发 %d" % (len(segs), len(done), len(todo), a.workers), flush=True)
    if not todo:
        return

    raw = load_raw_index()
    print("raw 索引 %d 条" % len(raw), flush=True)

    t0 = time.time()
    ok = fail = short = tok = 0
    with open(OUT, "a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(work, s, raw): s for s in todo}
            for n, fut in enumerate(as_completed(futs), 1):
                seq, sm, t, info = fut.result()
                if sm is None:
                    fail += 1
                    print("  ✗ seq=%d %s" % (seq, info[:80]), flush=True)
                    continue
                if info == "too_short":
                    short += 1
                ok += 1
                tok += t
                # 落盘前统一脱敏：摘要会进召回、会被注入上下文，
                # 所以凭证不能留在这层（冷层原文仍完整保留）。
                f.write(json.dumps({"seq": seq, "summary": scrub(sm)}, ensure_ascii=False) + "\n")
                f.flush()
                if n % 25 == 0:
                    el = time.time() - t0
                    print("  [%d/%d] %.0fs  %.2fs/段  ok=%d fail=%d tokens=%d" % (
                        n, len(todo), el, el/n, ok, fail, tok), flush=True)
    print("完成: ok=%d fail=%d short=%d tokens=%d 用时%.0fs" % (
        ok, fail, short, tok, time.time()-t0), flush=True)


if __name__ == "__main__":
    main()
