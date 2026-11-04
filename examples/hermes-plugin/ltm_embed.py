"""ltm-memory 的可选增强能力。

设计原则（很重要，别改坏）：
  1. 向量是加速器，不是地基。任何异常（没 key、网络断、额度耗尽、
     idx.db 里没 vec 表）都必须静默降级到纯词法，绝不能让记忆不可用。
  2. 写摘要必须脱敏。摘要会进召回、会被注入上下文，
     冷层原文（raw/）保留原文不动，但"加工品"不能带凭证。
  3. 这两块都不依赖 hermes 内部模块，只依赖标准库 + ltm2.py 的 CLI。

之所以做成独立文件而不是塞进 __init__.py：这个文件可以在宿主的
Python 环境外单独测试（python3 -c "import ltm_embed"）。
"""
import json
import math
import os
import sqlite3
import struct
import time
import urllib.request

# ---------------------------------------------------------------- 向量检索

# 任何 OpenAI 兼容的 /embeddings 端点都能接，用环境变量切换。
EMBED_API = os.environ.get("LTM_EMBED_API", "https://api.siliconflow.cn/v1/embeddings")
EMBED_MODEL = os.environ.get("LTM_EMBED_MODEL", "BAAI/bge-m3")
# key 从环境变量读，别写死在文件里（也支持从文件读，便于容器挂载）
EMBED_KEY_ENV = "LTM_EMBED_KEY"
EMBED_KEY_FILE = os.environ.get("LTM_EMBED_KEY_FILE", "")
EMBED_TIMEOUT = 3          # 秒：召回是热路径，超时就当没有向量
VEC_COOLDOWN = 300         # 秒：失败后这么久不再尝试，别每轮都卡一次
_last_fail = [0.0]         # 上次失败的时间戳（模块级，进程内有效）
VEC_MIN_SCORE = 0.68       # 低于这个余弦值的近邻不要
VEC_LIMIT = 20             # 最多带进来几条
VEC_BONUS = 0.6            # 加分系数：score * (1 + VEC_BONUS * cos)
LEXICAL_STRONG = 0.05      # 词法最高分 >= 这个值就不让向量介入

_cache = {}


def embed_query(text):
    """把查询编成向量。任何失败返回 None（绝不抛异常）。"""
    q = " ".join(str(text or "").split())
    if not q:
        return None
    if q in _cache:
        return _cache[q]
    # 刚失败过就别再试：宁可这一轮退化成纯词法，也不要让每轮都卡满超时
    if time.time() - _last_fail[0] < VEC_COOLDOWN:
        return None
    try:
        token = os.environ.get(EMBED_KEY_ENV, "").strip()
        if not token and EMBED_KEY_FILE:
            with open(EMBED_KEY_FILE, encoding="utf-8") as f:
                token = f.read().strip()
        if not token:
            return None
        body = json.dumps({"model": EMBED_MODEL, "input": [q]}).encode()
        req = urllib.request.Request(EMBED_API, data=body, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as r:
            data = json.load(r)
        vec = data["data"][0]["embedding"]
        _cache[q] = vec
        return vec
    except Exception:
        _last_fail[0] = time.time()
        return None


def _cos(a, b):
    s = na = nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return s / (math.sqrt(na) * math.sqrt(nb) + 1e-9)


def vector_hits(db_path, query, limit=VEC_LIMIT, min_score=VEC_MIN_SCORE):
    """查询的近邻 {id: cos}。任何异常返回 {}。"""
    try:
        qv = embed_query(query)
        if not qv:
            return {}
        if not os.path.exists(db_path):
            return {}
        con = sqlite3.connect(db_path, timeout=2)
        try:
            rows = con.execute("SELECT id, dim, v FROM vec").fetchall()
        finally:
            con.close()
        scored = []
        for rid, dim, blob in rows:
            try:
                v = struct.unpack("%df" % dim, blob)
            except Exception:
                continue
            scored.append((_cos(qv, v), str(rid)))
        scored.sort(reverse=True)
        return {rid: sc for sc, rid in scored[:limit] if sc >= min_score}
    except Exception:
        return {}


# ---------------------------------------------------------------- 摘要脱敏

SCRUB_PATTERNS = [
    (r"\b(?:root|admin|lenora|ubuntu|pi|astrbot)\s*[/:：]\s*[^\s，。、；）)\]】\"']{3,40}", "[已隐去]"),
    (r"(密码|口令|password|passwd)\s*[:：=]?\s*[A-Za-z0-9_@#.\-]{4,40}", r"\1 [已隐去]"),
    (r"sk-[A-Za-z0-9]{12,}", "sk-[已隐去]"),
    (r"(?i)(api[_\s-]?key|token|secret)\s*[:：=]?\s*[A-Za-z0-9_\-]{12,}", r"\1 [已隐去]"),
    (r"\b1[3-9]\d{9}\b", "[手机号]"),
    (r"\b[\w.\-]+@[\w\-]+\.[A-Za-z]{2,}\b", "[邮箱]"),
    (r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "[IP]"),
    (r"\[已隐去\]\]", "[已隐去]"),
]


def scrub(text):
    """凭证脱敏。纯函数、幂等。用于任何会进上下文的加工文本。"""
    import re as _re
    if not text:
        return text
    out = str(text)
    for pat, rep in SCRUB_PATTERNS:
        out = _re.sub(pat, rep, out, flags=_re.I)
    return out


if __name__ == "__main__":
    print("scrub:", scrub("账号 admin/Pa55w0rd 登录 10.0.0.5，key sk-abcdefghijklmnop"))
    db = os.environ.get("LTM_ROOT", "./ltm-data") + "/idx.db"
    print("vec hits:", len(vector_hits(db, "测试查询")))
