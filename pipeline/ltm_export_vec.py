"""把 idx.db 的 vec 表导出成 vec.npy + vec_ids.json，供召回时 mmap 直接读。

为什么：每次 recall 都从 sqlite 读 9,644 行 blob 再 struct.unpack，
实测 1.3 秒。改成内存映射的 .npy 后，加载接近免费。

可选降维：--dim 256 会用随机投影（不依赖 sklearn）把 1024 维压到 256 维，
计算量降 4 倍，精度损失在"找候选"这个用途上可以接受。
降维矩阵会一起存下来（proj.npy），查询时用同一个矩阵投影。
"""
import argparse
import json
import os
import sqlite3
import struct
import sys

try:
    import numpy as np
except Exception:
    sys.exit("需要 numpy：pip install numpy")


def build(root, target_dim=0, seed=20261104):
    db = os.path.join(root, "idx.db")
    if not os.path.exists(db):
        sys.exit("找不到 %s" % db)

    con = sqlite3.connect(db)
    rows = con.execute("SELECT id, dim, v FROM vec").fetchall()
    con.close()
    if not rows:
        sys.exit("vec 表是空的，先跑 build_vec.py")

    dims = {r[1] for r in rows}
    if len(dims) != 1:
        sys.exit("向量维度不一致: %s" % sorted(dims))
    dim = dims.pop()

    ids = [str(r[0]) for r in rows]
    mat = np.empty((len(rows), dim), dtype=np.float32)
    for i, (_, _, blob) in enumerate(rows):
        mat[i] = np.frombuffer(blob, dtype=np.float32)

    # 归一化：之后余弦 = 点积
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1e-9
    mat = mat / norms

    out_dim = dim
    if target_dim and 0 < target_dim < dim:
        # 随机投影（Johnson–Lindenstrauss），不依赖 sklearn
        rng = np.random.default_rng(seed)
        proj = rng.normal(0, 1.0 / (target_dim ** 0.5), size=(dim, target_dim)).astype(np.float32)
        mat = mat @ proj
        # 投影后重新归一化
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1e-9
        mat = mat / norms
        np.save(os.path.join(root, "proj.npy"), proj)
        out_dim = target_dim
        print("已做随机投影 %d -> %d 维" % (dim, target_dim))
    else:
        p = os.path.join(root, "proj.npy")
        if os.path.exists(p):
            os.remove(p)

    np.save(os.path.join(root, "vec.npy"), mat)
    with open(os.path.join(root, "vec_ids.json"), "w", encoding="utf-8") as f:
        json.dump({"dim": out_dim, "src_dim": dim, "ids": ids}, f, ensure_ascii=False)

    print("完成: %d 条 × %d 维" % (len(ids), out_dim))
    print("  %s  %.1f MB" % (os.path.join(root, "vec.npy"),
                             os.path.getsize(os.path.join(root, "vec.npy")) / 1024 / 1024))
    print("  %s" % os.path.join(root, "vec_ids.json"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.environ.get("LTM_ROOT", os.path.expanduser("~/.ltm")))
    ap.add_argument("--dim", type=int, default=0, help="目标维度；0 = 不降维")
    a = ap.parse_args()
    build(a.root, a.dim)
