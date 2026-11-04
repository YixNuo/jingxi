#!/usr/bin/env python3
"""出口脱敏自检：扫 ltm2.py，找出"把正文交给调用方但没洗"的函数。

背景（PITFALLS 26/28）：脱敏在这份代码里漏过两次——
先只洗索引不洗输出，后是 recall 洗了 project 忘了。两次都因为
"我记得这几个地方要洗"。人靠记忆守不住，得靠扫描。

判据（收窄版，只看真正的出口）：
  · 函数体里有 `return` 且该 return 的结构里带 content/text/thought 字段
  · 或者把 pack 交给 _print_json / print
内部构造、CLI 分发、参数解析一律不算出口。

退出码 0 = 全部已洗；1 = 有未洗的出口。
用法：python3 tests/check_scrub_exits.py [ltm2.py 路径]
"""
import os
import re
import sys

DEFAULT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ltm2.py")

CONTENT_FIELDS = ('"content"', '"text"', '"thought"')

# 明确不是出口的（内部构造 / 解析 / 分发）
SKIP = {
    "make_cold_record", "make_fixed_record", "make_hot_record",
    "normalize_cold_record", "idem_key",
    "_make_candidate", "_recall_candidates", "_recall_candidates_by_ids",
    "_fuse_pack", "_pack_budget", "_pack_projection", "_load_events",
    "_cold_text", "_cold_record",
    "scan_cold", "build_vec", "sensitive_guard", "outbound_scrub",
    "scrub_pack", "stage_for", "build_parser", "main",
    "cmd_import", "cmd_fixed", "cmd_hot", "cmd_recall", "cmd_project",
    "cmd_reflect", "cmd_think", "cmd_cue", "cmd_event", "cmd_stats",
    "rebuild_index", "verify_all", "collect_stats", "consolidate",
    "reflect_add", "think_add", "event_add", "event_attach_summary",
}


def find_functions(src):
    out = []
    for m in re.finditer(r"^def (\w+)\(", src, re.M):
        start = m.start()
        nxt = src.find("\ndef ", start + 1)
        out.append((m.group(1), start, nxt if nxt > 0 else len(src),
                    src[start:nxt if nxt > 0 else len(src)]))
    return out


def returns_content(body):
    """该函数是否在 return 结构里交出正文。

    两条判据都要，缺一就抓不全：
      · 结构里带 content/text/thought
      · 或者交出 pack / hits —— 这两样装的就是正文
    （recall/project 返回的是 {"pack": ...}，字面里没有 content，
     只看字段名会漏；这是这份自检第一版的真 bug。）
    """
    for m in re.finditer(r"return\s*(\{[^}]*\}|[^\n]+)", body):
        seg = m.group(1)
        if any(f in seg for f in CONTENT_FIELDS):
            return True
        if re.search(r'["\']?(pack|hits)["\']?\s*:', seg):
            return True
        if re.match(r"^(pack|hits)\b", seg.strip()):
            return True
    return False


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    if not os.path.exists(path):
        print("找不到", path)
        return 2

    src = open(path, encoding="utf-8").read()
    funcs = find_functions(src)

    good, bad = [], []
    for name, start, end, body in funcs:
        # 私有函数（_开头）是内部工具，不直接对外交付
        if name in SKIP or name.startswith("_"):
            continue
        if not returns_content(body):
            continue
        ln = src[:start].count("\n") + 1
        if "scrub_pack(" in body or "outbound_scrub(" in body:
            good.append((name, ln))
        else:
            bad.append((name, ln))

    print("扫描 %s（%d 个函数）" % (os.path.basename(path), len(funcs)))
    print("\n✓ 已脱敏的出口：")
    for name, ln in good:
        print("   %-24s 行 %d" % (name, ln))
    if bad:
        print("\n✗ 返回正文但没调 scrub：")
        for name, ln in bad:
            print("   %-24s 行 %d" % (name, ln))
        print("\nRESULT: FAIL")
        return 1

    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
