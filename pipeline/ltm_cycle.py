"""把"切段 → 摘要 → 回填事件 → 导向量"串成一条命令，可挂 cron。

设计：
  · 幂等：已切过的段、已有摘要、已建事件都跳过
  · 有失败不中断：某一环挂了，下一环照样跑
  · 输出一行结果，方便 cron 日志里看

用法：
  python3 ltm_cycle.py            # 全跑
  python3 ltm_cycle.py --no-vec   # 跳过向量（省 key）
"""
import argparse
import os
import subprocess
import sys
import time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("LTM_ROOT", os.path.expanduser("~/.ltm"))
ENV = dict(os.environ, LTM_ROOT=ROOT)


def run(name, cmd, timeout=900):
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=ENV, timeout=timeout, cwd="/root")
        tail = (r.stdout or r.stderr or "").strip().splitlines()
        last = tail[-1] if tail else ""
        return True, last[:160], time.time() - t0
    except subprocess.TimeoutExpired:
        return False, "超时", time.time() - t0
    except Exception as e:
        return False, str(e)[:120], time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-vec", action="store_true")
    a = ap.parse_args()

    steps = [
        ("切段", [sys.executable, os.path.join(SCRIPTS, "ltm_segment.py")]),
        ("摘要", [sys.executable, os.path.join(SCRIPTS, "ltm_summarize2.py"),
                  "--workers", "3"]),
        ("回填", [sys.executable, os.path.join(SCRIPTS, "ltm_backfill_events.py")]),
    ]
    if not a.no_vec:
        steps.append(("向量", [sys.executable, os.path.join(SCRIPTS, "ltm_build_vec.py")]))
        steps.append(("导出", [sys.executable, os.path.join(SCRIPTS, "ltm_export_vec.py"),
                             "--root", ROOT, "--dim", "256"]))

    ok_all = True
    for name, cmd in steps:
        ok, tail, dt = run(name, cmd)
        print("[%s] %-4s %5.1fs  %s" % ("✓" if ok else "✗", name, dt, tail), flush=True)
        if not ok:
            ok_all = False

    print("RESULT:", "ALL_OK" if ok_all else "PARTIAL")


if __name__ == "__main__":
    main()
