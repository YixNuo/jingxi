#!/usr/bin/env python3
"""把段 + 摘要回填成事件层（events.jsonl）。

对每个 seq：
  event add --from <source_ids> --topics <从标题/摘要抽的短词>
  然后 event summary <mid> --text <摘要>
最后 event advance 落 stage。
幂等：已存在 source_ids 完全相同的事件就跳过。
"""
import json, os, subprocess, sys, re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ltm_scrub import scrub   # 事件摘要也要脱敏

ROOT = os.environ.get("LTM_ROOT", os.path.expanduser("~/.ltm"))
CLI = [os.environ.get("PYTHON_BIN", "python3"),
       os.environ.get("LTM_CLI", os.path.join(os.path.dirname(os.path.dirname(
           os.path.abspath(__file__))), "ltm2.py")), "--root", ROOT]
SEGS = os.environ.get("LTM_SEGS", os.path.join(ROOT, "segments.jsonl"))
SUMS = os.environ.get("LTM_SUMS", os.path.join(ROOT, "summaries.jsonl"))
ENV = dict(os.environ, no_proxy="*", LTM_ROOT=ROOT)



def _ensure_files():
    """首次运行时 segments/summaries 还不存在，先建空文件再读。"""
    for path in (SEGS, SUMS):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if not os.path.exists(path):
                open(path, "a", encoding="utf-8").close()
        except Exception:
            pass

def load_segments():
    return {json.loads(l)["seq"]: json.loads(l)
            for l in open(SEGS, encoding="utf-8") if l.strip()}


def load_summaries():
    out = {}
    for ln in open(SUMS, encoding="utf-8"):
        try:
            r = json.loads(ln)
            out[r["seq"]] = r.get("summary") or ""
        except json.JSONDecodeError:
            pass
    return out


def existing_events():
    p = os.path.join(ROOT, "events.jsonl")
    if not os.path.exists(p):
        return set()
    keys = set()
    for ln in open(p, encoding="utf-8"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        src = r.get("source_ids") or []
        if src:
            keys.add(src[0])   # 用首个 source id 当指纹
    return keys


def topics_from(title, summary, limit=3):
    """从标题+摘要里抽 2-3 个短词当主题（纯规则）。"""
    text = (title or "") + " " + (summary or "")
    # 取引号/书名号里的，或 <> 之间的专名
    cand = re.findall(r"[《「【]([^》」】]{2,12})[》」】]", text)
    cand += re.findall(r"([A-Za-z][A-Za-z0-9_.\-]{2,20})", text)
    out = []
    for c in cand:
        c = c.strip()
        if c and c not in out and len(out) < limit:
            out.append(c)
    return out


def run(*a, timeout=120):
    r = subprocess.run(CLI + list(a), capture_output=True, text=True, env=ENV, timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def main():
    _ensure_files()
    segs = load_segments()
    sums = load_summaries()
    have = existing_events()
    print("段 %d / 摘要 %d / 已有事件 %d" % (len(segs), len(sums), len(have)))

    t0 = __import__("time").time()
    added = skipped = failed = 0
    for i, (seq, seg) in enumerate(sorted(segs.items()), 1):
        srcs = seg["source_ids"]
        if not srcs:
            continue
        if srcs[0] in have:
            skipped += 1
            continue
        sm = sums.get(seq, "")
        tp = topics_from(seg.get("title", ""), sm)
        args = ["event", "add", "--from", ",".join(srcs)]
        if tp:
            args += ["--topics", ",".join(tp)]
        code, out, err = run(*args)
        if code != 0 or not out.startswith("m_"):
            failed += 1
            if failed <= 5:
                print("  ✗ seq=%d %s" % (seq, (err or out)[:100]))
            continue
        mid = out.splitlines()[-1].strip()
        if sm:
            run("event", "summary", mid, "--text", scrub(sm)[:400])
        have.add(srcs[0])
        added += 1
        if i % 200 == 0:
            print("  [%d/%d] 已加 %d 跳过 %d 失败 %d  %.0fs" % (
                i, len(segs), added, skipped, failed, __import__("time").time()-t0), flush=True)

    print("回填完成: 新增 %d / 跳过 %d / 失败 %d  用时 %.0fs" % (
        added, skipped, failed, __import__("time").time()-t0))

    if added:
        print("推进 stage ...")
        code, out, err = run("event", "advance")
        print("  ", out[:200] or err[:200])


if __name__ == "__main__":
    main()
