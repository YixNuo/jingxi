"""摘要脱敏：生成之后、落盘之前，统一过这一把筛子。

放在独立模块里，供三条写路径共用：
  ltm_summarize2.py（批量生成）、ltm_backfill_events.py（回填事件）、
  以及任何以后新加的摘要来源。

规则本身不追求"精准识别"，追求"同一把筛子、无遗漏"——
前几次的教训是：靠人判断哪条脏，永远漏。
"""
import re

# 顺序重要：先清"账号/密码"这种复合形态，再清裸串
PATTERNS = [
    # 账号/密码、账号:密码（root/xxx、admin:xxx、含 IP 前缀）
    (r"\b(?:root|admin|ubuntu|pi|user)\s*[/:：]\s*[^\s，。、；）)\]】\"']{3,40}",
     "[已隐去]"),
    # 密码 xxx
    (r"(密码|口令|password|passwd)\s*[:：=]?\s*[A-Za-z0-9_@#.\-]{4,40}",
     r"\1 [已隐去]"),
    # API key / token
    (r"sk-[A-Za-z0-9]{12,}", "sk-[已隐去]"),
    (r"(?i)(api[_\s-]?key|token|secret)\s*[:：=]?\s*[A-Za-z0-9_\-]{12,}",
     r"\1 [已隐去]"),
    # 手机号
    (r"\b1[3-9]\d{9}\b", "[手机号]"),
    # 邮箱（登录名也算身份）
    (r"\b[\w.\-]+@[\w\-]+\.[A-Za-z]{2,}\b", "[邮箱]"),
    # 公网 IP（"已隐去"的残缺形态也顺手收掉）
    (r"\b47\.80\.60\.217\b", "[公网IP]"),
    (r"\[已隐去\]\]", "[已隐去]"),
]


def scrub(text):
    """把凭证类信息替换成占位符。纯函数，幂等。"""
    if not text:
        return text
    out = str(text)
    for pat, rep in PATTERNS:
        out = re.sub(pat, rep, out, flags=re.I)
    return out


if __name__ == "__main__":
    samples = [
        "admin/Pa55w0rd 登录成功",
        "密码 Pa55w0rd，公网 10.0.0.5",
        "admin@demo.com 是管理员",
        "sk-abcdefghijklmnop123",
        "普通的一句：今天做了什么",
    ]
    for s in samples:
        print("%-40s -> %s" % (s, scrub(s)))
