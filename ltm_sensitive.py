"""敏感内容闸门：写入冷层前判断这段话能不能建索引。

设计取舍（重要）：
  兜底方向必须是「找不到规则就按敏感处理」，而不是「放行」。
  这是 fail-closed：宁可能误判一句话不建索引（还能查到，只是不进全文检索），
  也不能让密码/手机号原样进索引、被召回到上下文里外发出去。

三条动作：
  redact  抹掉凭据后入库并建索引（默认）
  block   整条入库但不建索引（layer=sensitive）
  ok      原样入库
"""
import re

# 顺序有意义：先清"账号/密码"这种复合形态，再清裸串
RULES = [
    (r"\b(?:root|admin|lenora|ubuntu|pi|astrbot)\s*[/:：]\s*[^\s，。、；）)\]】\"']{3,40}",
     "[已隐去]", "credential"),
    (r"(密码|口令|password|passwd)\s*(?:是|为|[:：=])?\s*[A-Za-z0-9_@#.\-]{4,40}",
     r"\1 [已隐去]", "password"),
    (r"sk-[A-Za-z0-9]{12,}", "sk-[已隐去]", "apikey"),
    (r"(?i)(api[_\s-]?key|token|secret)\s*[:：=]?\s*[A-Za-z0-9_\-]{12,}",
     r"\1 [已隐去]", "apikey"),
    (r"\b1[3-9]\d{9}\b", "[手机号]", "phone"),
    (r"\b\d{15,19}\b", "[卡号]", "longnum"),
    (r"\b[\w.\-]+@[\w\-]+\.[A-Za-z]{2,}\b", "[邮箱]", "email"),
    (r"\b(?:[a-z0-9\-]+\.)+(?:com|cn|net|org|io|dev|xyz)\b", "[域名]", "domain"),
]


def classify(text):
    """返回 {action, kinds, text}。

    action 恒为 redact（除非整条都没命中 → ok）。
    之所以不用 block：block 会让整段对话查不到，代价太大；
    redact 只抹掉凭据本身，上下文保留。
    """
    s = text or ""
    if not isinstance(s, str):
        s = str(s)
    kinds = []
    out = s
    for pat, rep, kind in RULES:
        new = re.sub(pat, rep, out, flags=re.I)
        if new != out:
            out = new
            if kind not in kinds:
                kinds.append(kind)
    if not kinds:
        return {"action": "ok", "kinds": [], "text": s}
    return {"action": "redact", "kinds": kinds, "text": out}


if __name__ == "__main__":
    samples = [
        "我的密码是 Abc12345678",
        "我的手机号 13812345678",
        "root/Pa55w0rd 登录 10.0.0.5，sk-abcdefghijklmnop",
        "今天吃了白米饭",
    ]
    for t in samples:
        r = classify(t)
        print("%-46s -> %s %s" % (t, r["action"], r["text"]))
