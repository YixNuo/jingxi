"""README 示例全量验证：每条命令都要能真跑通。

做法：把 README 里所有 `python3 ltm2.py ...` 的命令抽出来，
在一个临时库里逐条执行，任何一条返回非 0 就报错。

这是防止"文档写了一条不存在的命令"——第一版 README 里
`event list` 就是死链（那时根本没有 list 子命令）。
"""
import os
import re
import shutil
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
CLI = os.path.join(ROOT, "ltm2.py")
TMP = "/tmp/ltm_readme_check"


def main():
    if not os.path.exists(README):
        print("找不到 README")
        return 2

    text = open(README, encoding="utf-8").read()
    cmds = []
    for m in re.finditer(r"^python3 ltm2\.py (.+)$", text, re.M):
        line = m.group(1).split("#")[0].strip()
        if line and line not in cmds:
            cmds.append(line)

    print("README 里找到 %d 条命令" % len(cmds))
    if not cmds:
        return 0

    if os.path.exists(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP, exist_ok=True)

    env = dict(os.environ, LTM_ROOT=TMP, no_proxy="*")
    # 先塞一条数据，否则 list 全是空的（空也算成功，但没意义）
    subprocess.run([sys.executable, CLI, "--root", TMP, "remember",
                    "测试数据：排骨汤要焯水", "--role", "user", "--session", "readme"],
                   capture_output=True, text=True, env=env)

    bad = []
    for c in cmds:
        parts = c.split()
        # 把引号里的内容还原（简单处理：shlex 更稳）
        import shlex
        try:
            argv = shlex.split(c)
        except ValueError:
            argv = parts
        r = subprocess.run([sys.executable, CLI, "--root", TMP] + argv,
                           capture_output=True, text=True, env=env, timeout=60)
        ok = r.returncode == 0
        print("  %s %s" % ("✓" if ok else "✗", c[:70]))
        if not ok:
            bad.append((c, (r.stderr or r.stdout)[:120]))

    if bad:
        print("\n✗ README 里有跑不通的命令：")
        for c, err in bad:
            print("   %s" % c)
            print("     %s" % err.replace("\n", " ")[:100])
        print("\nRESULT: FAIL")
        return 1

    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
