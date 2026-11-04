#!/usr/bin/env python3
"""ltm.py — 四层记忆系统 v2（第一部分）。

层与文件：
    FIXED      fixed.jsonl             性格/身份/称呼/边界，永不淘汰
    HOT        hot.jsonl               近期细节、常聊话题、未完成约定（可派生，保留供审计）
    COLD       raw/YYYY-MM-DD.jsonl    全量原始对话，只追加、不改写、不删除
    REFLECTION reflections.jsonl       反思与猜想，kind=reflection 且必须带 about

索引（idx.db）：
    cold_fts(rid UNINDEXED, content) / hot_fts / fixed_fts / refl_fts
    alias(alias, canonical, source)
    stats(key, value)   —— mention_count / last_mentioned 只能存在这里
    meta(key, value)

本文件实现第一部分：目录与文件结构、固定层、冷层、热层、索引建表，以及
remember / import / rebuild / verify / stats / fixed / hot / state 命令。
recall / project / reflect / consolidate / should_mention 只留占位，第二部分实现。
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import pathlib
import sqlite3
import struct
import sys
import time

SCHEMA_VERSION = "2"

FIXED_CATS = ("identity", "habit", "boundary")

DEFAULT_ROOT = os.environ.get("LTM_ROOT") or str(
    pathlib.Path(__file__).resolve().parent / "ltm_data"
)

TEXT_INDEXES = (
    ("cold_fts", "rid"),
    ("hot_fts", "hid"),
    ("fixed_fts", "fid"),
    ("refl_fts", "bid"),
)

INDEX_TO_LAYER = {
    "cold_fts": "cold",
    "hot_fts": "hot",
    "fixed_fts": "fixed",
    "refl_fts": "reflection",
}


class LtmError(Exception):
    """记忆库层面的可预期错误。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def now_ts():
    """返回当前时间戳（秒，浮点）。"""
    return time.time()


def ts_to_iso(ts):
    """把时间戳转成本地时区 ISO 字符串。"""
    try:
        dt = datetime.datetime.fromtimestamp(float(ts)).astimezone()
    except (TypeError, ValueError, OSError):
        dt = datetime.datetime.now().astimezone()
    return dt.isoformat(timespec="seconds")


def ts_to_day(ts):
    """把时间戳转成本地日期字符串 YYYY-MM-DD。"""
    try:
        dt = datetime.datetime.fromtimestamp(float(ts)).astimezone()
    except (TypeError, ValueError, OSError):
        dt = datetime.datetime.now().astimezone()
    return dt.strftime("%Y-%m-%d")


def iso_to_ts(text):
    """把 ISO 字符串转成时间戳，失败返回 None。"""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _new_id(prefix, ts, content, extra=""):
    """基于时间戳与内容生成确定性 id（同输入同输出，便于重建后结果一致）。"""
    payload = "{0}|{1:.6f}|{2}|{3}".format(prefix, float(ts), content, extra)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return "{0}_{1:013d}_{2}".format(prefix, int(float(ts) * 1000), digest)


def append_jsonl(path, obj):
    """把一条记录追加到 JSONL 文件（UTF-8，不转义中文）。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _jsonl_error(path, lineno, message, on_error):
    """按策略处理 JSONL 坏行：raise / warn / skip，或把消息追加进列表。"""
    text = "{0}:{1} {2}".format(path, lineno, message)
    if isinstance(on_error, list):
        on_error.append(text)
        return
    if on_error == "raise":
        raise LtmError(text)
    if on_error == "warn":
        print("警告：" + text, file=sys.stderr)


def iter_jsonl(path, on_error="raise"):
    """逐行读取 JSONL，产出 dict；空行跳过。

    on_error 取 "raise" / "warn" / "skip"，或传入 list 以收集错误文本。
    """
    path = pathlib.Path(path)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                _jsonl_error(path, lineno, "JSON 解析失败：{0}".format(exc), on_error)
                continue
            if not isinstance(obj, dict):
                _jsonl_error(path, lineno, "不是 JSON 对象", on_error)
                continue
            yield obj


def read_json_file(path, default):
    """读取 JSON 文件；缺失或损坏时返回 default。"""
    path = pathlib.Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path, obj):
    """原子写入 JSON 文件（先写临时文件再替换）。"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_value(text):
    """尝试按 JSON 解析值，失败则当普通字符串。"""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _char_ngrams(text, n=2):
    """返回文本的字符 n-gram 集合。"""
    s = " ".join(str(text).lower().split())
    if not s:
        return set()
    if len(s) <= n:
        return {s}
    return {s[i:i + n] for i in range(len(s) - n + 1)}


# ---------------------------------------------------------------------------
# 召回噪声闸：聊天里"你还记得吗""那次""上回"这类问句高频出现，
# 词法会把它们当强信号，把真正的记忆压下去。这里做两道闸：
#   1) 查询侧剥掉寒暄/回忆引子，只留实词
#   2) 候选侧给纯问句降权（问句本身不是记忆内容）
# ---------------------------------------------------------------------------

_CUE_PHRASES = (
    "你还记得", "你还记不记得", "记不记得", "还记得吗", "还记得嘛", "记不记得那个",
    "那次", "那次说", "上回", "上回说", "之前说", "以前说", "那次咱", "之前咱",
    "是什么来着", "是啥来着", "叫啥来着", "叫什么的", "来着",
    "主人喵", "主人", "喵", "吧", "呢", "吗", "啊", "呀", "哦", "嗯",
)


def _strip_cue(query):
    """从查询里剥掉回忆引子，留实词；剥完为空就退回原串。"""
    q = str(query or "")
    out = q
    for p in _CUE_PHRASES:
        out = out.replace(p, " ")
    out = " ".join(out.split())
    return out if len(out) >= 2 else q.strip()


def _is_plain_question(text):
    """很短、且以疑问尾巴结尾 = 纯问句（当噪声压）。"""
    t = " ".join(str(text or "").split())
    if not t:
        return False
    if len(t) > 40:
        return False
    tails = ("吗", "嘛", "呢", "?", "？", "吗？", "来着")
    return t.endswith(tails)


def lexical_score(query, text):
    """基于字符 bigram 的词法相关度（带 IDF 与长度归一），返回 >=0 的分数。

    比首版"覆盖率"更接近 BM25：
      · 每个 bigram 的权重 = IDF（越稀有的字对越重）
      · 覆盖率仍作为主因子，避免长文靠字多取胜
      · 查询串整体出现额外加分
    纯函数、不依赖索引，保证 rebuild 前后结果一致。
    """
    q = " ".join(str(query).lower().split())
    t = " ".join(str(text).lower().split())
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0 + 0.5 * min(1.0, len(q) / max(len(t), 1))
    qg = _char_ngrams(q)
    tg = _char_ngrams(t)
    if not qg or not tg:
        return 0.0
    hit = qg & tg
    if not hit:
        return 0.0
    # 覆盖率：命中比率
    cover = len(hit) / float(len(qg))
    # 稀有度：命中的 bigram 里，越少见的组合给的权重越高（用出现次数倒数近似 IDF）
    rarity = sum(1.0 / (1.0 + tg_count) for tg_count in
                 (_count_in(t, g) for g in hit)) / float(len(hit))
    # 长度惩罚：同样命中，短文本更相关
    length_pen = 1.0 / (1.0 + math.log(1.0 + len(t) / 200.0))
    return cover * (0.6 + 0.4 * rarity) * (0.5 + 0.5 * length_pen)


def _count_in(text, sub):
    """子串出现次数（用于稀有度近似）。"""
    if not sub:
        return 0
    n = 0
    start = 0
    while True:
        i = text.find(sub, start)
        if i < 0:
            return n
        n += 1
        start = i + 1


# ---------------------------------------------------------------------------
# 数据根目录
# ---------------------------------------------------------------------------


class Store:
    """封装数据根目录、文件布局与数据库连接。"""

    def __init__(self, root):
        """记录根目录并推导各层文件路径。"""
        self.root = pathlib.Path(root).expanduser().resolve()
        self.fixed = self.root / "fixed.jsonl"
        self.hot = self.root / "hot.jsonl"
        self.reflections = self.root / "reflections.jsonl"
        self.state_file = self.root / "state.json"
        self.idx = self.root / "idx.db"
        self.manifest = self.root / "manifest.json"
        self.raw_dir = self.root / "raw"
        self.logs_dir = self.root / "logs"

    def ensure(self):
        """创建目录结构和缺失的空文件（幂等）。"""
        self.root.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.fixed, self.hot, self.reflections):
            if not path.exists():
                path.touch()
        if not self.state_file.exists():
            write_json_atomic(self.state_file, default_state())
        if not self.manifest.exists():
            write_json_atomic(
                self.manifest,
                {"version": 2, "created_at": ts_to_iso(now_ts()), "runs": []},
            )

    def raw_file(self, ts):
        """返回给定时间戳对应的冷层日文件路径。"""
        return self.raw_dir / "{0}.jsonl".format(ts_to_day(ts))

    def connect(self):
        """打开并初始化 idx.db 连接。"""
        self.ensure()
        conn = sqlite3.connect(str(self.idx))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        init_schema(conn)
        return conn


def default_state():
    """构造默认状态结构。"""
    return {
        "mood": "neutral",
        "energy": 0.5,
        "focus": "",
        "open_loops": [],
        "updated_at": ts_to_iso(now_ts()),
    }


def load_state(store):
    """读取 state.json；缺失或损坏时返回默认结构。"""
    state = read_json_file(store.state_file, None)
    if not isinstance(state, dict):
        return default_state()
    merged = default_state()
    merged.update(state)
    if not isinstance(merged.get("open_loops"), list):
        merged["open_loops"] = []
    return merged


def save_state(store, state):
    """写回 state.json。"""
    write_json_atomic(store.state_file, state)


def log_event(store, kind, detail=""):
    """把一次操作追加到 logs/ltm.log（审计用）。"""
    try:
        store.logs_dir.mkdir(parents=True, exist_ok=True)
        line = "{0}\t{1}\t{2}\n".format(ts_to_iso(now_ts()), kind, detail)
        with (store.logs_dir / "ltm.log").open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def manifest_record(store, kind, processed, remaining, tokens_est):
    """向 manifest.json 追加一条运行记录（时间、处理数、剩余数、token 估算）。"""
    data = read_json_file(store.manifest, None)
    if not isinstance(data, dict):
        data = {"version": 2, "created_at": ts_to_iso(now_ts()), "runs": []}
    runs = data.get("runs")
    if not isinstance(runs, list):
        runs = []
    runs.append(
        {
            "kind": kind,
            "at": ts_to_iso(now_ts()),
            "processed": int(processed),
            "remaining": int(remaining),
            "tokens_est": int(tokens_est),
        }
    )
    data["version"] = 2
    data["runs"] = runs[-200:]
    write_json_atomic(store.manifest, data)


# ---------------------------------------------------------------------------
# 索引建表与读写
# ---------------------------------------------------------------------------


def _table_exists(conn, name):
    """判断 sqlite 中是否存在该表/虚表。"""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE name=?", (name,)
    ).fetchone()
    return row is not None


def create_text_index(conn, name, id_col):
    """创建文本索引表：优先 FTS5 虚表，不可用时退化为普通表（幂等）。"""
    if _table_exists(conn, name):
        return
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE {0} USING fts5({1} UNINDEXED, content, "
            "tokenize='unicode61')".format(name, id_col)
        )
    except sqlite3.OperationalError:
        conn.execute(
            "CREATE TABLE {0}({1} TEXT PRIMARY KEY, content TEXT)".format(name, id_col)
        )


def init_schema(conn):
    """建表：四层索引、alias、stats、meta（幂等）。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alias(alias TEXT, canonical TEXT, source TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)"
    )
    for name, id_col in TEXT_INDEXES:
        create_text_index(conn, name, id_col)
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS alias_alias_idx ON alias(alias)")
    except sqlite3.DatabaseError:
        pass
    conn.commit()


def index_put(conn, table, id_col, rid, content):
    """写入一条索引行；同 id 时先删后插，保证不重复。"""
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    row = None
    try:
        row = conn.execute(
            "SELECT rowid FROM {0} WHERE {1}=?".format(table, id_col), (rid,)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row:
        try:
            conn.execute(
                "DELETE FROM {0} WHERE rowid=?".format(table), (row[0],)
            )
        except sqlite3.OperationalError:
            pass
    conn.execute(
        "INSERT INTO {0}({1}, content) VALUES(?, ?)".format(table, id_col),
        (rid, content),
    )


def index_count(conn, table):
    """返回索引表行数，表不存在时返回 -1。"""
    try:
        return conn.execute("SELECT COUNT(*) FROM {0}".format(table)).fetchone()[0]
    except sqlite3.OperationalError:
        return -1


def index_search(conn, table, id_col, query, limit=50):
    """在索引表内做词法候选检索，返回 [(id, score)]。

    为保证中文短查询可用且结果可复现，这里使用字符 bigram 覆盖度打分；
    FTS5 虚表本身仍可被外部工具直接 MATCH 使用。
    """
    query = (query or "").strip()
    if not query:
        return []
    scored = []
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    except sqlite3.OperationalError:
        return []
    if id_col not in cols:
        return []
    # 有的表（event_fts）正文分 detail/summary 两列，拼起来当一条文本
    body_cols = [c for c in cols if c != id_col]
    if not body_cols:
        return []
    select = ", ".join([id_col] + body_cols)
    try:
        rows = conn.execute("SELECT %s FROM %s" % (select, table)).fetchall()
    except sqlite3.OperationalError:
        return []
    for row in rows:
        rid = row[0]
        content = " ".join(x for x in row[1:] if isinstance(x, str))
        if not isinstance(content, str):
            continue
        score = lexical_score(query, content)
        if score > 0.0:
            scored.append((rid, score))
    scored.sort(key=lambda item: (-item[1], str(item[0])))
    return scored[:limit]


def meta_set(conn, key, value):
    """写入 meta 表。"""
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
        (str(key), str(value)),
    )


def meta_get(conn, key, default=None):
    """读取 meta 表。"""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (str(key),)).fetchone()
    except sqlite3.OperationalError:
        return default
    return row[0] if row else default


def hot_stats_set(conn, hid, mention_count, last_mentioned):
    """把热层的 mention_count / last_mentioned 写入 stats 表（不改写原文）。"""
    conn.execute(
        "INSERT OR REPLACE INTO stats(key, value) VALUES(?, ?)",
        ("mention_count:{0}".format(hid), str(int(mention_count))),
    )
    conn.execute(
        "INSERT OR REPLACE INTO stats(key, value) VALUES(?, ?)",
        ("last_mentioned:{0}".format(hid), str(float(last_mentioned))),
    )


def hot_stats_get(conn, hid):
    """从 stats 表读取热层条目的 (mention_count, last_mentioned)。"""
    count, last = 1, 0.0
    try:
        row = conn.execute(
            "SELECT value FROM stats WHERE key=?", ("mention_count:{0}".format(hid),)
        ).fetchone()
        if row:
            count = int(float(row[0]))
        row = conn.execute(
            "SELECT value FROM stats WHERE key=?", ("last_mentioned:{0}".format(hid),)
        ).fetchone()
        if row:
            last = float(row[0])
    except (sqlite3.OperationalError, ValueError):
        pass
    return count, last


# ---------------------------------------------------------------------------
# 三层数据结构（固定 / 冷 / 热）
# ---------------------------------------------------------------------------


def make_cold_record(
    content,
    role="user",
    session="default",
    ts=None,
    scope="chat",
    platform="cli",
    meta=None,
    rid=None,
):
    """构造一条标准冷层记录（meta 可选，缺省为空标签）。"""
    ts = round(float(now_ts() if ts is None else ts), 6)
    if isinstance(meta, dict):
        norm_meta = {
            "place": meta.get("place"),
            "people": list(meta.get("people") or []),
            "things": list(meta.get("things") or []),
        }
    else:
        norm_meta = {"place": None, "people": [], "things": []}
    rid = rid or _new_id("r", ts, content, "{0}|{1}".format(role, session))
    return {
        "id": rid,
        "ts": ts,
        "iso": ts_to_iso(ts),
        "role": role,
        "content": content,
        "scope": scope,
        "session": session,
        "platform": platform,
        "layer": "talk",
        "meta": norm_meta,
    }


def normalize_cold_record(obj, default_session="default"):
    """把外部记录规范化为标准冷层记录；无法识别时返回 None。"""
    if not isinstance(obj, dict):
        return None
    content = obj.get("content")
    if content is None:
        content = obj.get("text")
    if content is None:
        content = obj.get("message")
    if not isinstance(content, str) or not content.strip():
        return None
    ts = obj.get("ts")
    if not isinstance(ts, (int, float)):
        alt = obj.get("iso") or obj.get("time") or obj.get("created_at")
        ts = iso_to_ts(alt) if isinstance(alt, str) else None
    if not isinstance(ts, (int, float)):
        ts = now_ts()
    role = obj.get("role") or "user"
    session = obj.get("session") or default_session or "default"
    rec = make_cold_record(
        content.strip(),
        role=str(role),
        session=str(session),
        ts=float(ts),
        scope=obj.get("scope") or "chat",
        platform=obj.get("platform") or "cli",
        meta=obj.get("meta"),
    )
    rid = obj.get("id")
    if isinstance(rid, str) and rid.strip():
        rec["id"] = rid.strip()
    return rec


def idem_key(rec):
    """冷层幂等键：(session, round(ts,3), role, sha256(content)[:16])。"""
    content = rec.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    try:
        ts = round(float(rec.get("ts") or 0.0), 3)
    except (TypeError, ValueError):
        ts = 0.0
    return "{0}|{1:.3f}|{2}|{3}".format(
        rec.get("session") or "", ts, rec.get("role") or "", digest
    )


def make_fixed_record(cat, content, ts=None):
    """构造一条固定层记录。"""
    if cat not in FIXED_CATS:
        raise LtmError("固定层分类非法：{0}".format(cat))
    ts = now_ts() if ts is None else float(ts)
    return {
        "id": _new_id("f", ts, content, cat),
        "cat": cat,
        "content": content,
        "created_at": ts_to_iso(ts),
    }


def make_hot_record(content, source_ids, topics=None, open_loop=False, ts=None):
    """构造一条热层记录（mention_count 初始为 1）。"""
    ts = round(float(now_ts() if ts is None else ts), 6)
    return {
        "id": _new_id("h", ts, content, ",".join(source_ids or [])),
        "ts": ts,
        "source_ids": list(source_ids or []),
        "content": content,
        "topics": list(topics or []),
        "last_mentioned": ts,
        "mention_count": 1,
        "open_loop": bool(open_loop),
        "importance": 0.5,
    }


def iter_cold(store, on_error="raise"):
    """按日期文件名顺序遍历冷层全部原始记录。"""
    for path in sorted(store.raw_dir.glob("*.jsonl")):
        for rec in iter_jsonl(path, on_error=on_error):
            yield rec


def load_cold_ids(store):
    """返回冷层全部 id 集合（坏行跳过）。"""
    ids = set()
    for rec in iter_cold(store, on_error="skip"):
        rid = rec.get("id")
        if isinstance(rid, str) and rid:
            ids.add(rid)
    return ids


def scan_cold(store):
    """扫描冷层，返回 (幂等键集合, id 集合)。"""
    keys, ids = set(), set()
    for rec in iter_cold(store, on_error="skip"):
        rid = rec.get("id")
        if isinstance(rid, str) and rid:
            ids.add(rid)
        keys.add(idem_key(rec))
    return keys, ids


# ---------------------------------------------------------------------------
# 命令：remember / import
# ---------------------------------------------------------------------------



# ===== 敏感内容闸门（写入口统一走这里）=====
_SENS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ltm_sensitive.py")
try:
    if _SENS_PATH not in sys.path:
        sys.path.insert(0, os.path.dirname(_SENS_PATH))
    from ltm_sensitive import classify as _sens_classify
except Exception:  # 兜底：找不到模块就按"通过"处理，绝不阻断写入
    def _sens_classify(text):
        return {"action": "ok", "kinds": [], "text": text}


def sensitive_guard(text):
    """返回 (最终文本, 是否可建索引, 命中的类别)。

    redact -> 原文抹掉凭据后入库并建索引
    block  -> 整条入库但不建索引（layer 标 sensitive）
    ok     -> 原样
    """
    r = _sens_classify(text or "")
    act = r.get("action", "ok")
    if act == "block":
        return text, False, r.get("kinds", [])
    if act == "redact":
        return r.get("text", text), True, r.get("kinds", [])
    return text, True, []


def cmd_remember(store, args):
    """把一条原始对话写入冷层并同步索引。"""
    store.ensure()
    content = (args.content or "").strip()
    if not content:
        raise LtmError("内容不能为空")
    clean, indexable, kinds = sensitive_guard(content)
    # 补历史对话时必须能指定真实时间，否则 15:30 的话会被当成"刚刚"，
    # 三档生命和排序全歪。
    _ts = getattr(args, "ts", None)
    rec = make_cold_record(content, role=args.role, session=args.session,
                           ts=float(_ts) if _ts else None)
    if kinds:
        rec["sensitive"] = kinds
    if not indexable:
        rec["layer"] = "sensitive"
    append_jsonl(store.raw_file(rec["ts"]), rec)
    if indexable:
        conn = store.connect()
        try:
            index_put(conn, "cold_fts", "rid", rec["id"], clean)
            conn.commit()
        finally:
            conn.close()
    log_event(store, "remember", rec["id"])
    print("已记录 {0} -> raw/{1}.jsonl".format(rec["id"], ts_to_day(rec["ts"])))
    return 0


def cmd_import(store, args):
    """从外部 JSONL 导入冷层记录，按幂等键去重。"""
    store.ensure()
    src = pathlib.Path(args.src).expanduser()
    if not src.exists():
        raise LtmError("文件不存在：{0}".format(src))
    keys, ids = scan_cold(store)
    imported, skipped, bad = 0, 0, 0
    conn = store.connect()
    try:
        with src.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    print(
                        "警告：{0}:{1} JSON 解析失败，已跳过".format(src, lineno),
                        file=sys.stderr,
                    )
                    continue
                rec = normalize_cold_record(obj, default_session=args.session)
                if rec is None:
                    bad += 1
                    print(
                        "警告：{0}:{1} 缺少 content，已跳过".format(src, lineno),
                        file=sys.stderr,
                    )
                    continue
                key = idem_key(rec)
                if key in keys:
                    skipped += 1
                    continue
                if rec["id"] in ids:
                    rec["id"] = _new_id(
                        "r", rec["ts"], rec["content"], "import:{0}".format(lineno)
                    )
                clean, indexable, kinds = sensitive_guard(rec["content"])
                if kinds:
                    rec["sensitive"] = kinds
                if not indexable:
                    rec["layer"] = "sensitive"
                append_jsonl(store.raw_file(rec["ts"]), rec)
                if indexable:
                    index_put(conn, "cold_fts", "rid", rec["id"], clean)
                keys.add(key)
                ids.add(rec["id"])
                imported += 1
        conn.commit()
    finally:
        conn.close()
    log_event(store, "import", "{0} imported={1}".format(src, imported))
    print(
        "导入完成：新增 {0} 条，重复跳过 {1} 条，坏行 {2} 条".format(
            imported, skipped, bad
        )
    )
    return 0


# ---------------------------------------------------------------------------
# 命令：fixed / hot
# ---------------------------------------------------------------------------


def cmd_fixed(store, args):
    """固定层操作：add（新增）与 list（列出）。"""
    store.ensure()
    if args.fixed_cmd == "add":
        content = (args.content or "").strip()
        if not content:
            raise LtmError("内容不能为空")
        rec = make_fixed_record(args.cat, content)
        append_jsonl(store.fixed, rec)
        conn = store.connect()
        try:
            index_put(conn, "fixed_fts", "fid", rec["id"], rec["content"])
            conn.commit()
        finally:
            conn.close()
        log_event(store, "fixed_add", rec["id"])
        print("已加入固定层 {0} [{1}]".format(rec["id"], rec["cat"]))
        return 0
    recs = list(iter_jsonl(store.fixed, on_error="warn"))
    if not recs:
        print("固定层为空")
        return 0
    for rec in recs:
        print(
            "{0}  [{1}]  {2}  ({3})".format(
                rec.get("id"), rec.get("cat"), rec.get("content"), rec.get("created_at")
            )
        )
    return 0


def cmd_hot(store, args):
    """热层操作：add（从冷层 id 派生一条提纯条目）。"""
    store.ensure()
    if args.hot_cmd != "add":
        raise LtmError("未知的 hot 子命令：{0}".format(args.hot_cmd))
    content = (args.content or "").strip()
    if not content:
        raise LtmError("内容不能为空")
    source_ids = [s.strip() for s in (args.from_ids or "").split(",") if s.strip()]
    if not source_ids:
        raise LtmError("--from 需要至少一个冷层 id")
    cold_ids = load_cold_ids(store)
    missing = [sid for sid in source_ids if sid not in cold_ids]
    if missing:
        raise LtmError("冷层不存在的 id：{0}".format(", ".join(missing)))
    topics = [t.strip() for t in (args.topics or "").split(",") if t.strip()]
    rec = make_hot_record(
        content, source_ids, topics=topics, open_loop=bool(args.open_loop)
    )
    append_jsonl(store.hot, rec)
    conn = store.connect()
    try:
        index_put(conn, "hot_fts", "hid", rec["id"], rec["content"])
        hot_stats_set(conn, rec["id"], rec["mention_count"], rec["last_mentioned"])
        conn.commit()
    finally:
        conn.close()
    log_event(store, "hot_add", rec["id"])
    print(
        "已加入热层 {0}（来源 {1} 条，未完成={2}）".format(
            rec["id"], len(source_ids), rec["open_loop"]
        )
    )
    return 0


# ---------------------------------------------------------------------------
# 命令：rebuild
# ---------------------------------------------------------------------------


def remove_index_files(store):
    """删除 idx.db 及其 WAL 附属文件。"""
    for suffix in ("", "-wal", "-shm"):
        path = pathlib.Path(str(store.idx) + suffix)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                raise LtmError("无法删除 {0}：{1}".format(path, exc))


def rebuild_index(store):
    """清空并重建 idx.db：四层索引 + stats（从 hot.jsonl 恢复）+ meta。"""
    store.ensure()
    remove_index_files(store)
    counts = {"fixed": 0, "hot": 0, "cold": 0, "reflection": 0}
    seen = {"fixed": set(), "hot": set(), "cold": set(), "reflection": set()}
    conn = store.connect()
    try:
        with conn:
            for rec in iter_jsonl(store.fixed, on_error="warn"):
                fid = rec.get("id")
                content = rec.get("content")
                if not isinstance(fid, str) or not fid or not isinstance(content, str):
                    continue
                if fid in seen["fixed"]:
                    continue
                seen["fixed"].add(fid)
                index_put(conn, "fixed_fts", "fid", fid, content)
                counts["fixed"] += 1
            for rec in iter_jsonl(store.hot, on_error="warn"):
                hid = rec.get("id")
                content = rec.get("content")
                if not isinstance(hid, str) or not hid or not isinstance(content, str):
                    continue
                if hid in seen["hot"]:
                    continue
                seen["hot"].add(hid)
                index_put(conn, "hot_fts", "hid", hid, content)
                try:
                    mention_count = int(rec.get("mention_count") or 1)
                except (TypeError, ValueError):
                    mention_count = 1
                last = rec.get("last_mentioned")
                if not isinstance(last, (int, float)):
                    last = rec.get("ts")
                if not isinstance(last, (int, float)):
                    last = now_ts()
                hot_stats_set(conn, hid, mention_count, float(last))
                counts["hot"] += 1
            for rec in iter_jsonl(store.reflections, on_error="warn"):
                bid = rec.get("id")
                content = rec.get("content")
                if not isinstance(bid, str) or not bid or not isinstance(content, str):
                    continue
                if bid in seen["reflection"]:
                    continue
                seen["reflection"].add(bid)
                index_put(conn, "refl_fts", "bid", bid, content)
                counts["reflection"] += 1
            for rec in iter_cold(store, on_error="warn"):
                rid = rec.get("id")
                content = rec.get("content")
                if not isinstance(rid, str) or not rid or not isinstance(content, str):
                    continue
                if rid in seen["cold"]:
                    continue
                seen["cold"].add(rid)
                index_put(conn, "cold_fts", "rid", rid, content)
                counts["cold"] += 1
            meta_set(conn, "schema_version", SCHEMA_VERSION)
            meta_set(conn, "rebuilt_at", ts_to_iso(now_ts()))
            meta_set(conn, "counts", json.dumps(counts, ensure_ascii=False))
    finally:
        conn.close()
    total = sum(counts.values())
    manifest_record(store, "rebuild", total, 0, 0)
    log_event(store, "rebuild", json.dumps(counts, ensure_ascii=False))
    return counts


def cmd_rebuild(store, args):
    """重建索引并汇报各层计数。"""
    counts = rebuild_index(store)
    print("已重建 idx.db：")
    for name in ("fixed", "hot", "cold", "reflection"):
        print("  {0:<11}{1}".format(name, counts[name]))
    return 0


# ---------------------------------------------------------------------------
# 命令：verify
# ---------------------------------------------------------------------------


def verify_all(store):
    """执行完整一致性检查，返回 {"ok","errors","warnings","counts"}。"""
    errors, warnings = [], []
    counts = {"fixed": 0, "hot": 0, "cold": 0, "reflection": 0}

    for path in (store.fixed, store.hot, store.reflections,
                 store.state_file, store.manifest):
        if not path.exists():
            errors.append("缺少文件：{0}".format(path))
    if not store.raw_dir.exists():
        errors.append("缺少冷层目录：{0}".format(store.raw_dir))

    cold_ids = set()
    idem_seen = {}
    for path in sorted(store.raw_dir.glob("*.jsonl")):
        for rec in iter_jsonl(path, on_error=errors):
            rid = rec.get("id")
            if not isinstance(rid, str) or not rid:
                errors.append("{0}: 冷层记录缺少 id".format(path.name))
                continue
            if not rid.startswith("r_"):
                warnings.append("{0}: 冷层 id 前缀不规范：{1}".format(path.name, rid))
            if rid in cold_ids:
                errors.append("冷层 id 重复：{0}".format(rid))
            cold_ids.add(rid)
            if not isinstance(rec.get("content"), str) or not rec["content"].strip():
                errors.append("冷层记录内容为空：{0}".format(rid))
            if rec.get("layer") != "talk":
                warnings.append("冷层记录 layer 非 talk：{0}".format(rid))
            if "mention_count" in rec or "last_mentioned" in rec:
                errors.append(
                    "冷层记录禁止写 mention_count/last_mentioned：{0}".format(rid)
                )
            meta = rec.get("meta")
            if meta is not None and not isinstance(meta, dict):
                warnings.append("冷层 meta 不是对象：{0}".format(rid))
            key = idem_key(rec)
            if key in idem_seen:
                warnings.append(
                    "冷层幂等键重复：{0} / {1}".format(idem_seen[key], rid)
                )
            else:
                idem_seen[key] = rid
            counts["cold"] += 1

    hot_ids = set()
    hot_stats = {}
    for rec in iter_jsonl(store.hot, on_error=errors):
        hid = rec.get("id")
        if not isinstance(hid, str) or not hid:
            errors.append("热层记录缺少 id")
            continue
        if hid in hot_ids:
            errors.append("热层 id 重复：{0}".format(hid))
        hot_ids.add(hid)
        if not isinstance(rec.get("content"), str) or not rec["content"].strip():
            errors.append("热层记录内容为空：{0}".format(hid))
        source_ids = rec.get("source_ids")
        if source_ids is None:
            warnings.append("热层缺少 source_ids：{0}".format(hid))
            source_ids = []
        if not isinstance(source_ids, list):
            errors.append("热层 source_ids 不是列表：{0}".format(hid))
            source_ids = []
        for sid in source_ids:
            if sid not in cold_ids:
                errors.append(
                    "热层 {0} 的 source_id 不存在于冷层：{1}".format(hid, sid)
                )
        mention_count = rec.get("mention_count")
        if not isinstance(mention_count, int) or mention_count < 1:
            warnings.append("热层 mention_count 异常：{0}".format(hid))
            mention_count = 1
        last = rec.get("last_mentioned")
        if not isinstance(last, (int, float)):
            warnings.append("热层 last_mentioned 缺失：{0}".format(hid))
            last = rec.get("ts")
        if not isinstance(last, (int, float)):
            last = 0.0
        hot_stats[hid] = (int(mention_count), float(last))
        counts["hot"] += 1

    fixed_ids = set()
    for rec in iter_jsonl(store.fixed, on_error=errors):
        fid = rec.get("id")
        if not isinstance(fid, str) or not fid:
            errors.append("固定层记录缺少 id")
            continue
        if fid in fixed_ids:
            errors.append("固定层 id 重复：{0}".format(fid))
        fixed_ids.add(fid)
        if rec.get("cat") not in FIXED_CATS:
            errors.append("固定层 cat 非法：{0} -> {1}".format(fid, rec.get("cat")))
        if not isinstance(rec.get("content"), str) or not rec["content"].strip():
            errors.append("固定层内容为空：{0}".format(fid))
        counts["fixed"] += 1

    refl_ids = set()
    for rec in iter_jsonl(store.reflections, on_error=errors):
        bid = rec.get("id")
        if not isinstance(bid, str) or not bid:
            errors.append("反射层记录缺少 id")
            continue
        if bid in refl_ids:
            errors.append("反射层 id 重复：{0}".format(bid))
        refl_ids.add(bid)
        if rec.get("kind") != "reflection":
            errors.append("反射层 kind 必须为 reflection：{0}".format(bid))
        about = rec.get("about")
        if not isinstance(about, list) or not about:
            errors.append("反射层必须有 about：{0}".format(bid))
            about = []
        for rid in about:
            if rid not in cold_ids:
                errors.append(
                    "反射层 {0} 的 about 指向不存在的冷层 id：{1}".format(bid, rid)
                )
        confidence = rec.get("confidence")
        if not isinstance(confidence, (int, float)) or not (
            0.0 <= float(confidence) <= 1.0
        ):
            warnings.append("反射层 confidence 异常：{0}".format(bid))
        if not isinstance(rec.get("content"), str) or not rec["content"].strip():
            errors.append("反射层内容为空：{0}".format(bid))
        counts["reflection"] += 1

    state = read_json_file(store.state_file, None)
    if not isinstance(state, dict):
        errors.append("state.json 不是对象")
        state = {}
    for key in ("mood", "energy", "focus", "open_loops", "updated_at"):
        if key not in state:
            warnings.append("state.json 缺少字段：{0}".format(key))
    if not isinstance(state.get("open_loops"), list):
        warnings.append("state.json 的 open_loops 不是列表")

    if not store.idx.exists():
        errors.append("idx.db 不存在，请运行 rebuild")
    else:
        conn = None
        try:
            conn = sqlite3.connect(str(store.idx))
            for name, _id_col in TEXT_INDEXES:
                if not _table_exists(conn, name):
                    errors.append("idx.db 缺少表：{0}".format(name))
                    continue
                rows = index_count(conn, name)
                expect = counts[INDEX_TO_LAYER[name]]
                if rows != expect:
                    errors.append(
                        "{0} 行数 {1} 与文件条数 {2} 不一致，请运行 rebuild".format(
                            name, rows, expect
                        )
                    )
            for name in ("alias", "stats", "meta"):
                if not _table_exists(conn, name):
                    errors.append("idx.db 缺少表：{0}".format(name))
            if _table_exists(conn, "stats"):
                stats = {}
                for key, value in conn.execute("SELECT key, value FROM stats"):
                    stats[key] = value
                for hid, pair in hot_stats.items():
                    mention_key = "mention_count:{0}".format(hid)
                    last_key = "last_mentioned:{0}".format(hid)
                    if stats.get(mention_key) != str(int(pair[0])):
                        errors.append(
                            "stats 表 mention_count 与 hot 文件不一致：{0}".format(hid)
                        )
                    if stats.get(last_key) != str(float(pair[1])):
                        errors.append(
                            "stats 表 last_mentioned 与 hot 文件不一致：{0}".format(hid)
                        )
        except sqlite3.DatabaseError as exc:
            errors.append("idx.db 无法读取：{0}".format(exc))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": counts,
    }


def cmd_verify(store, args):
    """校验四层文件与索引的一致性，输出问题列表。"""
    report = verify_all(store)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1
    print("校验结果：{0}".format("通过" if report["ok"] else "失败"))
    print(
        "  条目：fixed={0} hot={1} cold={2} reflection={3}".format(
            report["counts"]["fixed"],
            report["counts"]["hot"],
            report["counts"]["cold"],
            report["counts"]["reflection"],
        )
    )
    for err in report["errors"]:
        print("  [错误] " + err)
    for warn in report["warnings"]:
        print("  [警告] " + warn)
    print(
        "  错误 {0} 项，警告 {1} 项".format(
            len(report["errors"]), len(report["warnings"])
        )
    )
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------------------
# 命令：stats
# ---------------------------------------------------------------------------


def collect_stats(store):
    """收集四层记忆与索引的统计信息。"""
    store.ensure()
    counts = {"fixed": 0, "hot": 0, "cold": 0, "reflection": 0}
    bad = []
    sessions, roles = {}, {}

    days = sorted(path.stem for path in store.raw_dir.glob("*.jsonl"))
    for path in sorted(store.raw_dir.glob("*.jsonl")):
        for rec in iter_jsonl(path, on_error=bad):
            counts["cold"] += 1
            session = rec.get("session") or ""
            role = rec.get("role") or ""
            sessions[session] = sessions.get(session, 0) + 1
            roles[role] = roles.get(role, 0) + 1

    hot_records = list(iter_jsonl(store.hot, on_error=bad))
    counts["hot"] = len(hot_records)
    hot_open_loops = sum(1 for rec in hot_records if rec.get("open_loop"))
    hot_top = sorted(
        hot_records,
        key=lambda rec: (-int(rec.get("mention_count") or 1), str(rec.get("id"))),
    )[:5]

    counts["fixed"] = len(list(iter_jsonl(store.fixed, on_error=bad)))
    counts["reflection"] = len(list(iter_jsonl(store.reflections, on_error=bad)))

    idx_info = {"exists": store.idx.exists(), "bytes": 0, "tables": {}, "meta": {}}
    if store.idx.exists():
        idx_info["bytes"] = store.idx.stat().st_size
        conn = None
        try:
            conn = sqlite3.connect(str(store.idx))
            for name, _id_col in TEXT_INDEXES:
                idx_info["tables"][name] = index_count(conn, name)
            if _table_exists(conn, "alias"):
                idx_info["tables"]["alias"] = index_count(conn, "alias")
            if _table_exists(conn, "meta"):
                for key, value in conn.execute("SELECT key, value FROM meta"):
                    idx_info["meta"][key] = value
        except sqlite3.DatabaseError:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    manifest = read_json_file(store.manifest, {})
    runs = manifest.get("runs") if isinstance(manifest, dict) else []
    if not isinstance(runs, list):
        runs = []

    return {
        "root": str(store.root),
        "counts": counts,
        "cold_files": len(days),
        "cold_range": [days[0], days[-1]] if days else None,
        "sessions": sessions,
        "roles": roles,
        "bad_lines": len(bad),
        "hot_open_loops": hot_open_loops,
        "hot_top": [
            {
                "id": rec.get("id"),
                "content": rec.get("content"),
                "mention_count": int(rec.get("mention_count") or 1),
            }
            for rec in hot_top
        ],
        "idx": idx_info,
        "manifest": {
            "runs": len(runs),
            "last": runs[-1] if runs else None,
        },
        "state": load_state(store),
    }


def cmd_stats(store, args):
    """打印四层记忆库的统计信息。"""
    data = collect_stats(store)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print("== LTM v2 stats ==")
    print("root       : {0}".format(data["root"]))
    print("fixed      : {0}".format(data["counts"]["fixed"]))
    print(
        "hot        : {0}（未完成 {1}）".format(
            data["counts"]["hot"], data["hot_open_loops"]
        )
    )
    cold_line = "cold       : {0}".format(data["counts"]["cold"])
    if data["cold_range"]:
        cold_line += "（{0} 个日文件，{1} ~ {2}）".format(
            data["cold_files"], data["cold_range"][0], data["cold_range"][1]
        )
    else:
        cold_line += "（无日文件）"
    print(cold_line)
    print("reflection : {0}".format(data["counts"]["reflection"]))
    if data["sessions"]:
        print(
            "sessions   : "
            + ", ".join(
                "{0}={1}".format(key or "(空)", value)
                for key, value in sorted(data["sessions"].items())
            )
        )
    if data["roles"]:
        print(
            "roles      : "
            + ", ".join(
                "{0}={1}".format(key or "(空)", value)
                for key, value in sorted(data["roles"].items())
            )
        )
    idx = data["idx"]
    print(
        "idx.db     : {0}{1}".format(
            "存在" if idx["exists"] else "缺失",
            "，{0} bytes".format(idx["bytes"]) if idx["exists"] else "",
        )
    )
    if idx["tables"]:
        print(
            "             "
            + " ".join(
                "{0}={1}".format(name, value)
                for name, value in sorted(idx["tables"].items())
            )
        )
    if idx["meta"].get("rebuilt_at"):
        print("rebuilt_at : {0}".format(idx["meta"]["rebuilt_at"]))
    last = data["manifest"]["last"]
    if last:
        print(
            "manifest   : {0} 次运行，最近 {1} @ {2}（processed={3}）".format(
                data["manifest"]["runs"],
                last.get("kind"),
                last.get("at"),
                last.get("processed"),
            )
        )
    else:
        print("manifest   : 0 次运行")
    state = data["state"]
    print(
        "state      : mood={0} energy={1} focus={2} open_loops={3}".format(
            state.get("mood"),
            state.get("energy"),
            state.get("focus") or "-",
            len(state.get("open_loops") or []),
        )
    )
    if data["hot_top"]:
        print("hot top    :")
        for item in data["hot_top"]:
            print(
                "  [{0}] x{1} {2}".format(
                    item["mention_count"], item["mention_count"], item["content"]
                )
            )
    if data["bad_lines"]:
        print("坏行       : {0}".format(data["bad_lines"]))
    return 0


# ---------------------------------------------------------------------------
# 命令：state
# ---------------------------------------------------------------------------


def cmd_state(store, args):
    """查看或修改 state.json（--set k=v，可重复）。"""
    store.ensure()
    state = load_state(store)
    if args.set:
        for pair in args.set:
            if "=" not in pair:
                raise LtmError("--set 需要 k=v 形式：{0}".format(pair))
            key, value = pair.split("=", 1)
            key = key.strip()
            if not key:
                raise LtmError("--set 的键不能为空")
            state[key] = parse_value(value)
        state["updated_at"] = ts_to_iso(now_ts())
        if not isinstance(state.get("open_loops"), list):
            state["open_loops"] = []
        save_state(store, state)
        log_event(store, "state_set", ",".join(sorted(args.set)))
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# 第二部分占位接口
# ---------------------------------------------------------------------------




















# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


COMMON = argparse.ArgumentParser(add_help=False)
COMMON.add_argument(
    "--root",
    default=argparse.SUPPRESS,
    help="数据根目录（默认取 LTM_ROOT 或脚本同级的 ltm_data）",
)



# ===== 第二部分 =====
# ===== 第二部分：recall / project / should_mention / reflect_add / consolidate =====

import inspect
import json
import math
import os
import sqlite3
import uuid
from pathlib import Path

_RRF_K = 60
_MAX_ITEM_TOKENS = 400
_MIN_FIT_TOKENS = 16
_HOT_DECAY_DAYS = 14.0
_HOT_FREQ_CAP = 2.0
_HOT_LOOP_BOOST = 1.5
_MENTION_FRESH_SECONDS = 7 * 86400
_CONSOLIDATE_IDLE_SECONDS = 60 * 86400
_DUP_JACCARD = 0.9
_COLD_SCORE_FLOOR = 0.5
_REFLECTION_NOTE = "（我还没有确认过）"

EVENT_LAYER = "event"            # 事件层：摘要参与召回，专治"换个说法就找不到"
_EVENT_SUMMARY_NOTE = "简化自原始对话"

_LAYER_ORDER = ("fixed", "hot", EVENT_LAYER, "cold", "reflection")
_LAYER_WEIGHTS = {"fixed": 1.3, "hot": 1.0, EVENT_LAYER: 1.0, "cold": 1.0, "reflection": 0.6}
_LAYER_INDEX = {
    "fixed": ("fixed_fts", "fid"),
    "hot": ("hot_fts", "hid"),
    EVENT_LAYER: ("event_fts", "mid"),
    "cold": ("cold_fts", "rid"),
    "reflection": ("refl_fts", "bid"),
}
_LAYER_ALIAS = {
    "fixed": "fixed", "fix": "fixed", "pin": "fixed",
    "hot": "hot", "warm": "hot",
    "event": "event", "events": "event", "episode": "event", "episodic": "event",
    "cold": "cold", "raw": "cold",
    "reflection": "reflection", "reflections": "reflection",
    "reflect": "reflection", "refl": "reflection", "insight": "reflection",
}


def _store_root(store):
    """把 store 解析成根目录 Path。"""
    if isinstance(store, Path):
        return store
    if isinstance(store, (str, os.PathLike)):
        return Path(store)
    for attr in ("root", "path", "dir", "store"):
        val = getattr(store, attr, None)
        if isinstance(val, (str, os.PathLike)):
            return Path(val)
    if isinstance(store, dict):
        for key in ("root", "path", "dir", "store"):
            val = store.get(key)
            if isinstance(val, (str, os.PathLike)):
                return Path(val)
    raise LtmError("无法确定 store 根目录")


def _open_db(root):
    if hasattr(root, "idx"):
        path = str(root.idx)
    else:
        path = str(Path(root) / "idx.db")
    con = sqlite3.connect(path)
    try:
        init_schema(con)
        con.commit()
    except Exception:
        pass
    return con


def _param_count(fn):
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return 0
    count = 0
    for param in sig.parameters.values():
        if param.kind == param.VAR_POSITIONAL:
            return 99
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            count += 1
    return count


def _make_id(prefix, ts, content, extra=None):
    n = _param_count(_new_id)
    try:
        if n >= 4:
            return str(_new_id(prefix, ts, content, extra))
        if n == 3:
            return str(_new_id(prefix, ts, content))
        if n == 2:
            return str(_new_id(prefix, ts))
        return str(_new_id(prefix))
    except TypeError:
        return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def _iter_jsonl_safe(path):
    try:
        return iter_jsonl(path)
    except TypeError:
        return iter_jsonl(path, None)


def _jsonl_records(path):
    out = {}
    path = Path(path)
    if not path.exists():
        return out
    try:
        for rec in _iter_jsonl_safe(path):
            if isinstance(rec, dict):
                rid = rec.get("id")
                if rid is not None:
                    out[str(rid)] = rec
    except Exception:
        return out
    return out


def _to_int(value, default=0):
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _to_ts(value, default=0):
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return default
        try:
            return int(float(text))
        except ValueError:
            pass
        try:
            return int(iso_to_ts(text))
        except Exception:
            return default
    return default


def _estimate_tokens(text):
    return len(text) // 2 + 1


def _truncate_tokens(text, limit):
    limit = int(limit)
    if limit <= 0:
        return "", 0
    max_chars = limit * 2 - 1
    if len(text) <= max_chars:
        return text, _estimate_tokens(text)
    cut = text[:max_chars]
    return cut, _estimate_tokens(cut)


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    union = len(a | b)
    if not union:
        return 0.0
    return len(a & b) / float(union)


def _is_duplicate(grams, kept_grams, threshold=None):
    limit = _DUP_JACCARD if threshold is None else float(threshold)
    for other in kept_grams:
        if _jaccard(grams, other) > limit:
            return True
    return False


def _normalize_layers(layers):
    all_layers = set(_LAYER_ORDER)
    if not layers:
        return all_layers
    raw = []
    if isinstance(layers, str):
        for part in layers.replace(",", " ").replace(";", " ").split():
            raw.append(part)
    else:
        try:
            for part in layers:
                if isinstance(part, str):
                    raw.append(part)
        except TypeError:
            raw = [str(layers)]
    out = set()
    for part in raw:
        key = part.strip().lower()
        if key in ("all", "*", "any"):
            return all_layers
        layer = _LAYER_ALIAS.get(key)
        if layer:
            out.add(layer)
    return out or all_layers


def _normalize_ids(about):
    if about is None:
        return []
    if isinstance(about, str):
        raw = about.replace(",", " ").replace(";", " ").split()
    else:
        try:
            raw = [str(x).strip() for x in about]
        except TypeError:
            raw = [str(about).strip()]
    out = []
    for rid in raw:
        rid = str(rid).strip()
        if rid and rid not in out:
            out.append(rid)
    return out


def _layer_paths(root):
    root = Path(root)
    return {
        "fixed": str(root / "fixed.jsonl"),
        "hot": str(root / "hot.jsonl"),
        "event": str(root / "events.jsonl"),
        "cold": str(root / "raw"),
        "reflection": str(root / "reflections.jsonl"),
        "index": str(root / "idx.db"),
    }


def _fts_ids(con, table, id_col, query, limit):
    try:
        rows = index_search(con, table, id_col, query, limit)
    except Exception:
        return []
    if not rows:
        return []
    out = []
    seen = set()
    for row in rows:
        rid = None
        if isinstance(row, dict):
            rid = row.get(id_col)
            if rid is None:
                rid = row.get("id")
        elif isinstance(row, (tuple, list)):
            if len(row) >= 2 and isinstance(row[0], (int, float)) and not isinstance(row[0], bool) \
                    and isinstance(row[1], str):
                rid = row[1]
            elif row:
                rid = row[0]
        else:
            rid = row
        if rid is None:
            continue
        rid = str(rid)
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
    if limit:
        out = out[:int(limit)]
    return out


def _hot_stats(con, hid):
    stats = {}
    for key in ("mention_count", "last_mentioned"):
        try:
            stats[key] = hot_stats_get(con, hid, key)
        except Exception:
            stats[key] = None
    return stats


def _hot_factor(last_mentioned, mention_count, open_loop, now):
    days = max(0.0, (now - last_mentioned) / 86400.0)
    decay = 0.5 ** (days / _HOT_DECAY_DAYS)
    freq = 1.0 + math.log(1.0 + max(0, mention_count))
    if freq > _HOT_FREQ_CAP:
        freq = _HOT_FREQ_CAP
    loop = _HOT_LOOP_BOOST if open_loop else 1.0
    return decay * freq * loop


def _make_candidate(con, now, layer, rid, base_weight, rank,
                    hot_recs, fixed_recs, refl_recs, cold_recs, event_recs=None):
    rid = str(rid)
    note = ""
    extra = {}
    if layer == "fixed":
        rec = fixed_recs.get(rid)
        if not rec:
            return None
        content = str(rec.get("content") or "")
        weight = base_weight
        extra["ts"] = _to_ts(rec.get("created_at"), 0)
        if rec.get("cat"):
            extra["cat"] = rec.get("cat")
    elif layer == "hot":
        rec = hot_recs.get(rid)
        if not rec:
            return None
        stats = _hot_stats(con, rid)
        mc = _to_int(stats.get("mention_count"), None)
        if mc is None:
            mc = _to_int(rec.get("mention_count"), 0)
        last = _to_ts(stats.get("last_mentioned"), 0)
        if not last:
            last = _to_ts(rec.get("last_mentioned"), 0)
        if not last:
            last = _to_ts(rec.get("ts"), 0)
        if not last:
            last = now
        open_loop = bool(rec.get("open_loop"))
        weight = base_weight * _hot_factor(last, mc, open_loop, now)
        content = str(rec.get("content") or "")
        extra["ts"] = last
        extra["last_mentioned"] = last
        extra["mention_count"] = mc
        extra["open_loop"] = open_loop
        if rec.get("source_ids"):
            extra["source_ids"] = rec.get("source_ids")
    elif layer == "cold":
        rec = cold_recs.get(rid)
        if not rec:
            return None
        content = str(rec.get("content") or "")
        weight = base_weight
        extra["ts"] = _to_ts(rec.get("ts"), 0)
        if rec.get("role"):
            extra["role"] = rec.get("role")
        if rec.get("session"):
            extra["session"] = rec.get("session")
    elif layer == EVENT_LAYER:
        rec = event_recs.get(rid) if event_recs else None
        if not rec:
            return None
        # 优先给摘要（压缩过的、能兜住换词），没有摘要退回原文
        content = (rec.get("summary") or "").strip() or str(rec.get("detail") or "")
        stage = rec.get("stage") or STAGE_DETAIL
        if stage == STAGE_COLD:
            note = "cold"
            content = (rec.get("summary") or "").strip()
        elif stage == STAGE_SUMMARY:
            note = _EVENT_SUMMARY_NOTE
        else:
            note = ""
        weight = base_weight * 0.9
        extra["ts"] = _to_ts(rec.get("last_mentioned") or rec.get("ts"), 0)
        extra["event_stage"] = stage
        if rec.get("source_ids"):
            extra["source_ids"] = rec.get("source_ids")
    else:
        rec = refl_recs.get(rid)
        if not rec:
            return None
        content = str(rec.get("content") or "")
        weight = base_weight
        note = _REFLECTION_NOTE
        extra["ts"] = _to_ts(rec.get("ts"), 0)
        if rec.get("about"):
            extra["about"] = rec.get("about")
        if rec.get("confidence") is not None:
            extra["confidence"] = rec.get("confidence")
    if not content:
        return None
    item = {
        "id": rid,
        "layer": layer,
        "content": content,
        "score": weight / float(_RRF_K + rank),
        "note": note,
        "truncated": False,
    }
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# 向量路（可选加速器）
# 设计原则：向量只加分、不减分；算不出来/没建向量/离线，都直接跳过，
# 词法路照常工作。向量挂了记忆库也得好用。
# ---------------------------------------------------------------------------

EMBED_API = os.environ.get("LTM_EMBED_API", "https://api.siliconflow.cn/v1/embeddings")
EMBED_MODEL = os.environ.get("LTM_EMBED_MODEL", "BAAI/bge-m3")
EMBED_KEY_ENV = "LTM_EMBED_KEY"
EMBED_KEY_FILE = os.environ.get("LTM_EMBED_KEY_FILE", "")
_EMBED_CACHE = {}


def _embed_query(text):
    """把查询编成向量。失败一律返回 None，绝不抛给调用方。"""
    key = " ".join(str(text or "").split())
    if not key:
        return None
    if key in _EMBED_CACHE:
        return _EMBED_CACHE[key]
    try:
        import urllib.request
        token = os.environ.get(EMBED_KEY_ENV, "").strip()
        if not token and EMBED_KEY_FILE:
            try:
                token = open(EMBED_KEY_FILE, encoding="utf-8").read().strip()
            except OSError:
                return None
        if not token:
            return None
        body = json.dumps({"model": EMBED_MODEL, "input": [key]}).encode()
        req = urllib.request.Request(EMBED_API, data=body, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        vec = data["data"][0]["embedding"]
        _EMBED_CACHE[key] = vec
        return vec
    except Exception:
        return None


def _cos(a, b):
    s = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return s / (math.sqrt(na) * math.sqrt(nb) + 1e-9)


def _vector_hits(con, query, limit=20):
    """向量近邻。任何异常都返回 {}，不影响词法。"""
    try:
        qv = _embed_query(query)
        if not qv:
            return {}
        try:
            rows = con.execute("SELECT id, dim, v FROM vec").fetchall()
        except sqlite3.OperationalError:
            return {}
        scored = []
        for rid, dim, blob in rows:
            try:
                v = struct.unpack("%df" % dim, blob)
            except Exception:
                continue
            scored.append((_cos(qv, v), str(rid)))
        scored.sort(reverse=True)
        return {rid: sc for sc, rid in scored[:limit] if sc > 0.68}
    except Exception:
        return {}


def _recall_candidates(root, query, k, layers):
    # 统一用 Store：iter_cold 等既有函数接收的是 Store 对象，不是 Path
    store = root if isinstance(root, Store) else Store(root)
    root = store.root
    active = _normalize_layers(layers)
    fused = {}
    con = store.connect()
    try:
        hits = {}
        for layer in _LAYER_ORDER:
            if layer not in active:
                continue
            table, id_col = _LAYER_INDEX[layer]
            hits[layer] = _fts_ids(con, table, id_col, query, k)

        hot_recs = _jsonl_records(store.hot) if hits.get("hot") else {}
        fixed_recs = _jsonl_records(store.fixed) if hits.get("fixed") else {}
        refl_recs = _jsonl_records(store.reflections) if hits.get("reflection") else {}
        event_recs = {}
        if hits.get(EVENT_LAYER):
            try:
                event_recs = _load_events(store)
            except Exception:
                event_recs = {}
        cold_recs = {}
        if hits.get("cold"):
            wanted = set(hits["cold"])
            try:
                for rec in iter_cold(store):
                    if isinstance(rec, dict):
                        rid = rec.get("id")
                        if rid is not None and str(rid) in wanted:
                            cold_recs[str(rid)] = rec
            except Exception:
                cold_recs = {}

        now = now_ts()
        for layer in _LAYER_ORDER:
            ids = hits.get(layer) or []
            base = _LAYER_WEIGHTS.get(layer, 1.0)
            for rank, rid in enumerate(ids, start=1):
                item = _make_candidate(con, now, layer, rid, base, rank,
                                       hot_recs, fixed_recs, refl_recs, cold_recs,
                                       event_recs)
                if item is None:
                    continue
                prev = fused.get(item["id"])
                if prev is None or item["score"] > prev["score"]:
                    fused[item["id"]] = item
    finally:
        con.close()
    # 向量路：只加分，不减分。算不出来就当没有这条路。
    con2 = None
    try:
        con2 = store.connect()
        vec_hits = _vector_hits(con2, query)
    except Exception:
        vec_hits = {}
    finally:
        if con2 is not None:
            try:
                con2.close()
            except Exception:
                pass

    # 向量只在"词法明显不够用"时才介入。
    # 判据看最高分而不是条数：一堆 0.01 分的候选中看不中用，
    # 那种情况正是语义该出手的时候。
    top_score = max((float(it.get("score") or 0) for it in fused.values()), default=0.0)
    lexical_strong = top_score >= 0.05
    if lexical_strong:
        vec_hits = {}

    if vec_hits:
        # 已有的候选：向量相似度越高，分数乘得越大（最多 2.5 倍）
        for it in fused.values():
            sc = vec_hits.get(str(it["id"]))
            if sc:
                it["score"] = float(it["score"]) * (1.0 + 0.6 * float(sc))
                it["vec_score"] = round(float(sc), 4)
        # 词法没捞到、但向量很自信的：补进来（只补摘要和冷层）
        # 补进来的候选必须有字面交集：纯语义近邻容易把"加载到显卡"拉进来
        q_grams = set(_char_ngrams(str(query).lower(), 2))
        bonus, _ = _recall_candidates_by_ids(store, list(vec_hits.keys()), now_ts())
        for it in bonus:
            iid = str(it["id"])
            if iid in fused:
                continue
            body = str(it.get("content") or "").lower()
            if q_grams and not any(g in body for g in q_grams):
                continue
            sc = vec_hits.get(iid, 0.0)
            it["score"] = 0.02 * (1.0 + 0.6 * float(sc))
            it["vec_score"] = round(float(sc), 4)
            fused[iid] = it

    cands = sorted(fused.values(), key=lambda it: (-it["score"], it["id"]))
    meta = {"paths": _layer_paths(root), "fused": len(cands), "dropped": 0,
            "vec": len(vec_hits)}
    return cands, meta


def _recall_candidates_by_ids(store, ids, now, limit=20):
    """按 id 直接补候选（向量路用）。只补 event 和 cold，取不到就跳过。"""
    out = []
    if not ids:
        return out, 0
    wanted = set(str(i) for i in ids)
    # event
    try:
        events = _load_events(store)
    except Exception:
        events = {}
    for mid, rec in events.items():
        if mid not in wanted:
            continue
        summ = (rec.get("summary") or "").strip()
        if not summ:
            continue
        out.append({
            "id": mid, "layer": EVENT_LAYER, "content": summ,
            "note": _EVENT_SUMMARY_NOTE, "truncated": False,
            "ts": _to_ts(rec.get("last_mentioned") or rec.get("ts"), 0),
            "event_stage": rec.get("stage") or STAGE_DETAIL,
        })
    # cold
    got = {o["id"] for o in out}
    rest = wanted - got
    if rest:
        try:
            for rec in iter_cold(store):
                if not isinstance(rec, dict):
                    continue
                rid = str(rec.get("id") or "")
                if rid not in rest:
                    continue
                content = str(rec.get("content") or "")
                if not content:
                    continue
                out.append({
                    "id": rid, "layer": "cold", "content": content,
                    "note": "", "truncated": False,
                    "ts": _to_ts(rec.get("ts"), 0),
                })
        except Exception:
            pass
    return out[:limit], len(out)


def _pack_budget(cands, budget):
    pack = []
    used = 0
    dropped = 0
    seen_ids = set()          # 同一条记忆只装一次（三档生命下同一 id 可能被多路捞到）
    seen_grams = []           # 内容近似的只留第一条（防止同一段话反复占位）
    for item in cands:
        content = str(item.get("content") or "")
        if not content:
            dropped += 1
            continue
        iid = str(item.get("id") or "")
        if iid and iid in seen_ids:
            dropped += 1
            continue
        grams = set(_char_ngrams(content, 3))
        if _is_duplicate(grams, seen_grams, threshold=0.92):
            dropped += 1
            continue
        truncated = False
        tokens = _estimate_tokens(content)
        if tokens > _MAX_ITEM_TOKENS:
            content, tokens = _truncate_tokens(content, _MAX_ITEM_TOKENS)
            truncated = True
        if used + tokens > budget:
            dropped += 1
            continue
        entry = dict(item)
        entry["content"] = content
        entry["truncated"] = truncated
        pack.append(entry)
        used += tokens
        if iid:
            seen_ids.add(iid)
        seen_grams.append(grams)
    return pack, used, dropped


def _fuse_pack(first, rest, budget):
    """把"先装的摘要"和"补的原话"合成一份，仍按分数排，避免摘要一坨原话一坨。"""
    merged = []
    seen = set()
    for item in list(first) + list(rest):
        iid = str(item.get("id") or "")
        if iid and iid in seen:
            continue
        if iid:
            seen.add(iid)
        merged.append(item)
    order = {"thought": 0, EVENT_LAYER: 1, "hot": 2, "cold": 3, "fixed": 4, "reflection": 5}
    merged.sort(key=lambda it: (-float(it.get("score") or 0), order.get(str(it.get("layer")), 9)))
    out, used = [], 0
    for item in merged:
        t = _estimate_tokens(str(item.get("content") or ""))
        if used + t > budget:
            continue
        out.append(item)
        used += t
    return out, used


def _pack_projection(cands, budget):
    pack = []
    kept_grams = []
    used = 0
    removed_dup = 0
    truncated_count = 0
    dropped = 0
    for item in cands:
        content = str(item.get("content") or "")
        if not content:
            dropped += 1
            continue
        grams = set(_char_ngrams(content, 3))
        if _is_duplicate(grams, kept_grams):
            removed_dup += 1
            continue
        remaining = budget - used
        if remaining < _MIN_FIT_TOKENS:
            dropped += 1
            continue
        truncated = False
        tokens = _estimate_tokens(content)
        if tokens > _MAX_ITEM_TOKENS:
            content, tokens = _truncate_tokens(content, _MAX_ITEM_TOKENS)
            truncated = True
        if tokens > remaining:
            content, tokens = _truncate_tokens(content, remaining)
            truncated = True
        entry = dict(item)
        entry["content"] = content
        entry["truncated"] = truncated
        pack.append(entry)
        kept_grams.append(grams)
        used += tokens
        if truncated:
            truncated_count += 1
    return pack, used, removed_dup, truncated_count, dropped


def _touch_hot(root, items):
    hot_items = [it for it in items if it.get("layer") == "hot"]
    if not hot_items:
        return
    con = _open_db(root)
    try:
        now = now_ts()
        for item in hot_items:
            hid = str(item.get("id") or "")
            if not hid:
                continue
            stats = _hot_stats(con, hid)
            mc = _to_int(stats.get("mention_count"), None)
            if mc is None:
                mc = _to_int(item.get("mention_count"), 0)
            try:
                hot_stats_set(con, hid, "mention_count", mc + 1)
                hot_stats_set(con, hid, "last_mentioned", now)
            except Exception:
                continue
        con.commit()
    finally:
        con.close()


def _write_manifest(root, kind, processed, remaining, tokens_est):
    payload = {
        "ts": now_ts(),
        "kind": kind,
        "processed": int(processed),
        "remaining": int(remaining),
        "tokens_est": int(tokens_est),
    }
    five = (root, kind, int(processed), int(remaining), int(tokens_est))
    two = (root, payload)
    first, second = (five, two) if _param_count(manifest_record) >= 5 else (two, five)
    try:
        manifest_record(*first)
    except TypeError:
        manifest_record(*second)


def recall(store, query, budget_tokens=1200, k=10, layers=None):
    root = _store_root(store)
    text = "" if query is None else str(query).strip()
    if not text:
        raise LtmError("recall 需要非空 query")
    try:
        budget = int(budget_tokens)
    except (TypeError, ValueError):
        budget = 1200
    budget = max(0, budget)
    try:
        top_k = int(k)
    except (TypeError, ValueError):
        top_k = 10
    top_k = max(1, top_k)

    # 查询侧先剥掉"你还记得""那次"这类引子，只留实词去撞索引；
    # 剥空了就退回原串（比如用户只说了"那次"）。
    probe = _strip_cue(text)

    cands, meta = _recall_candidates(root, text, top_k, layers)

    # 思考层：想过的事也随事一起回来。
    # 用事件 id 去 thought.about 里找，只捞贴着这些事件的思考。
    try:
        store_obj = root if isinstance(root, Store) else Store(root)
        hit_ids = {str(c.get("id")) for c in cands}
        think_hits = []
        for t in _think_iter(store_obj):
            about = [str(x) for x in (t.get("about") or [])]
            if about and hit_ids.intersection(about):
                ts = _to_ts(t.get("ts"), 0)
                think_hits.append({
                    "id": str(t.get("id")),
                    "layer": "thought",
                    "content": str(t.get("thought") or ""),
                    "score": 0.02,
                    "note": "我当时想的（未确认）",
                    "truncated": False,
                    "ts": ts,
                    "topic": t.get("topic") or "",
                    "confidence": t.get("confidence"),
                })
        think_hits.sort(key=lambda x: -x["ts"])
        cands = think_hits[:2] + cands
    except Exception:
        pass
    if probe and probe != text:
        extra, _ = _recall_candidates(root, probe, top_k, layers)
        seen = {str(c.get("id")) for c in cands}
        for c in extra:
            if str(c.get("id")) not in seen:
                cands.append(c)
        cands.sort(key=lambda it: (-it["score"], it["id"]))

    # 候选侧：纯问句降权（"你还记得吗"这种不是记忆内容）
    for c in cands:
        if _is_plain_question(c.get("content")):
            c["score"] = float(c.get("score") or 0) * 0.25
    cands.sort(key=lambda it: (-it["score"], it["id"]))

    # 先装事件摘要（短、覆盖面广），剩余预算再给冷层原话。
    # 人想起旧事也是先有个大概，细节对不上才翻原文。
    summary_cands = [c for c in cands if c.get("layer") == EVENT_LAYER]
    other_cands = [c for c in cands if c.get("layer") != EVENT_LAYER]
    first, used1, drop1 = _pack_budget(summary_cands, int(budget * 0.55))
    rest, used2, drop2 = _pack_budget(other_cands + summary_cands, budget - used1)
    pack, used = _fuse_pack(first, rest, budget)
    dropped = drop1 + drop2
    _touch_hot(root, pack)
    meta["dropped"] = dropped
    return {"pack": pack, "tokens_used": used, "budget": budget, "meta": meta}


def project(store, query, token_budget=1200, k=10):
    root = _store_root(store)
    text = "" if query is None else str(query).strip()
    if not text:
        raise LtmError("project 需要非空 query")
    try:
        budget = int(token_budget)
    except (TypeError, ValueError):
        budget = 1200
    budget = max(0, budget)
    try:
        top_k = int(k)
    except (TypeError, ValueError):
        top_k = 10
    top_k = max(1, top_k)

    cands, meta = _recall_candidates(root, text, top_k, None)
    pack, used, removed_dup, truncated, dropped = _pack_projection(cands, budget)
    _touch_hot(root, pack)
    meta["dropped"] = dropped
    return {
        "pack": pack,
        "tokens_used": used,
        "budget": budget,
        "meta": meta,
        "projection": {"removed_dup": removed_dup, "truncated": truncated},
    }


def should_mention(item, context=None):
    if not isinstance(item, dict):
        return "cite"
    layer = item.get("layer")
    if layer == "reflection":
        return "ask"
    if layer == "hot":
        if bool(item.get("open_loop")):
            last = _to_ts(item.get("last_mentioned"), 0)
            if not last:
                last = _to_ts(item.get("ts"), 0)
            if last and (now_ts() - last) <= _MENTION_FRESH_SECONDS:
                return "cite"
        return "cite"
    if layer == "cold":
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        if score < _COLD_SCORE_FLOOR:
            return "hold"
        return "cite"
    return "cite"


def reflect_add(store, content, about, confidence=0.6):
    root = _store_root(store)
    if not isinstance(content, str) or not content.strip():
        raise LtmError("反射内容不能为空")
    ids = _normalize_ids(about)
    if not ids:
        raise LtmError("about 不能为空，需要至少一个冷层 id")
    known = set(load_cold_ids(root) or ())
    missing = [rid for rid in ids if rid not in known]
    if missing:
        raise LtmError("about 中的 id 不在冷层: " + ", ".join(missing[:5]))
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        raise LtmError("confidence 必须是数字")
    if conf < 0.0:
        conf = 0.0
    if conf > 1.0:
        conf = 1.0

    ts = now_ts()
    rid = _make_id("rf", ts, content, ids)
    rec = {
        "id": rid,
        "ts": ts,
        "kind": "reflection",
        "about": ids,
        "content": content,
        "confidence": conf,
    }
    append_jsonl(Path(root) / "reflections.jsonl", rec)
    con = _open_db(root)
    try:
        index_put(con, "refl_fts", "bid", rid, content)
        con.commit()
    finally:
        con.close()
    return rid


def consolidate(store, max_calls=20, max_tokens=20000):
    root = _store_root(store)
    try:
        call_cap = max(0, int(max_calls))
    except (TypeError, ValueError):
        call_cap = 20
    try:
        token_cap = max(0, int(max_tokens))
    except (TypeError, ValueError):
        token_cap = 20000

    hot_recs = _jsonl_records(Path(root) / "hot.jsonl")
    candidates = []
    con = _open_db(root)
    try:
        now = now_ts()
        for hid, rec in hot_recs.items():
            stats = _hot_stats(con, hid)
            mc = _to_int(stats.get("mention_count"), None)
            if mc is None:
                mc = _to_int(rec.get("mention_count"), 0)
            last = _to_ts(stats.get("last_mentioned"), 0)
            if not last:
                last = _to_ts(rec.get("last_mentioned"), 0)
            if not last:
                last = _to_ts(rec.get("ts"), 0)
            if mc == 1 and (now - last) > _CONSOLIDATE_IDLE_SECONDS:
                candidates.append((last, hid, rec))
    finally:
        con.close()

    candidates.sort(key=lambda row: (row[0], row[1]))
    processed = 0
    tokens_est = 0
    touched = []
    for last, hid, rec in candidates:
        if processed >= call_cap:
            break
        cost = _estimate_tokens(str(rec.get("content") or ""))
        if processed > 0 and tokens_est + cost > token_cap:
            break
        processed += 1
        tokens_est += cost
        touched.append(hid)

    remaining = max(0, len(candidates) - processed)
    _write_manifest(root, "consolidate", processed, remaining, tokens_est)
    return {
        "processed": processed,
        "remaining": remaining,
        "candidates": len(candidates),
        "tokens_est": tokens_est,
        "ids": touched,
    }


def _cli_arg(args, names, default=None):
    for name in names:
        if isinstance(args, dict):
            val = args.get(name)
        else:
            val = getattr(args, name, None)
        if val is not None:
            return val
    return default


def _cli_int(args, names, default):
    val = _cli_arg(args, names, None)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_recall(store, args):
    query = _cli_arg(args, ("query", "q", "text"), None)
    if isinstance(query, (list, tuple)):
        query = " ".join(str(part) for part in query)
    if not query:
        raise LtmError("recall 需要查询词")
    budget = _cli_int(args, ("budget_tokens", "budget", "tokens"), 1200)
    top_k = _cli_int(args, ("k", "top", "limit"), 10)
    layers = _cli_arg(args, ("layers", "layer"), None)
    _print_json(recall(store, query, budget, top_k, layers))
    return 0


def cmd_project(store, args):
    query = _cli_arg(args, ("query", "q", "text"), None)
    if isinstance(query, (list, tuple)):
        query = " ".join(str(part) for part in query)
    if not query:
        raise LtmError("project 需要查询词")
    budget = _cli_int(args, ("token_budget", "budget_tokens", "budget", "tokens"), 1200)
    top_k = _cli_int(args, ("k", "top", "limit"), 10)
    _print_json(project(store, query, budget, top_k))
    return 0


def cmd_reflect(store, args):
    content = _cli_arg(args, ("content", "text", "body"), None)
    if not content:
        raise LtmError("reflect 需要 --content")
    about = _cli_arg(args, ("about", "about_ids", "refs"), None)
    conf = _cli_arg(args, ("confidence", "conf"), 0.6)
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = 0.6
    rid = reflect_add(store, content, about, conf)
    _print_json({"id": rid, "about": _normalize_ids(about), "confidence": conf})
    return 0


def cmd_consolidate(store, args):
    max_calls = _cli_int(args, ("max_calls", "calls", "limit"), 20)
    max_tokens = _cli_int(args, ("max_tokens", "tokens", "budget"), 20000)
    _print_json(consolidate(store, max_calls, max_tokens))
    return 0



# ===== 第三部分：THOUGHT + cue_gate =====
# ===== ltm2.py 追加部分：THOUGHT 思考层 + 线索门控 cue_gate =====
import json
import math
import os
import sqlite3
import time
from pathlib import Path

THOUGHT_FILENAME = "thoughts.jsonl"
_CUE_MAX = 5


# ---------------------------------------------------------------
# THOUGHT 思考记录层（5 个函数）
# ---------------------------------------------------------------

def _think_root(store):
    """定位 store 根目录。"""
    root = getattr(store, "root", None)
    if root is not None:
        return Path(root)
    idx = getattr(store, "idx", None)
    if idx is not None:
        return Path(idx).parent
    raise ValueError("无法确定 store 根目录")


def _think_path(store):
    """思考层文件路径：root / thoughts.jsonl"""
    return _think_root(store) / THOUGHT_FILENAME


def _think_iter(store):
    """读出全部思考记录（原始顺序）。"""
    out = []
    for rec in iter_jsonl(_think_path(store)):
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def _think_norm_ids(value):
    """把 'a,b' / ['a','b'] / None 统一成去重后的 id 列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("，", ",").split(",")
    else:
        raw = [str(x) for x in value]
    out = []
    for item in raw:
        item = str(item).strip()
        if item and item not in out:
            out.append(item)
    return out


def _think_clamp_conf(value):
    try:
        conf = float(value)
    except (TypeError, ValueError):
        raise ValueError("confidence 必须是 0.0-1.0 之间的数字")
    if math.isnan(conf) or conf < 0.0 or conf > 1.0:
        raise ValueError("confidence 必须在 0.0-1.0 之间")
    return conf


def _think_cold_ids(store):
    ids = set()
    for rec in iter_cold(store):
        rid = rec.get("id")
        if rid:
            ids.add(rid)
    return ids


def _think_sort_key(rec):
    try:
        ts = float(rec.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return (ts, str(rec.get("id") or ""))


def _think_idx_con(store):
    idx = getattr(store, "idx", None)
    if idx is None:
        idx = _think_root(store) / "idx.db"
    con = sqlite3.connect(str(idx))
    con.row_factory = sqlite3.Row
    return con


def _think_ensure_table(con):
    """幂等创建 thought_fts(tid UNINDEXED, thought)。返回是否新建。"""
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='thought_fts'"
    ).fetchone()
    if row:
        return False
    for sql in (
        "CREATE VIRTUAL TABLE IF NOT EXISTS thought_fts "
        "USING fts5(tid UNINDEXED, thought, content)",
        "CREATE VIRTUAL TABLE IF NOT EXISTS thought_fts "
        "USING fts4(tid UNINDEXED, thought, content)",
    ):
        try:
            con.execute(sql)
            con.commit()
            return True
        except sqlite3.OperationalError:
            continue
    raise LtmError("无法创建 thought_fts 表")


def _think_index_put(store, tid, text):
    con = _think_idx_con(store)
    try:
        _think_ensure_table(con)
        con.execute("DELETE FROM thought_fts WHERE tid=?", (tid,))
        con.execute("INSERT INTO thought_fts(tid, thought, content) VALUES(?,?,?)",
                    (tid, text, text))
        con.commit()
    finally:
        con.close()


def think_add(store, content, about, topic="", based_on=None, confidence=0.6):
    """新增一条思考。about 必填且每个 id 必须存在于冷层。返回 tid。"""
    text = (content or "").strip()
    if not text:
        raise ValueError("thought 内容不能为空")

    about_ids = _think_norm_ids(about)
    if not about_ids:
        raise ValueError("about 不能为空：思考至少要挂一条冷记忆")

    cold_ids = _think_cold_ids(store)
    event_ids = set()
    try:
        event_ids = set(_load_events(store).keys())
    except Exception:
        event_ids = set()
    # about 可以挂冷层原话，也可以挂事件——想一件事，想的往往是"那件事"而不是某句话
    missing_cold = [cid for cid in about_ids
                    if cid not in cold_ids and cid not in event_ids]
    if missing_cold:
        raise ValueError("about 引用的记忆不存在: " + ", ".join(missing_cold))

    based_ids = _think_norm_ids(based_on)
    if based_ids:
        known_t = set(r.get("id") for r in _think_iter(store))
        missing_t = [tid for tid in based_ids if tid not in known_t]
        if missing_t:
            raise ValueError("based_on 引用的思考不存在: " + ", ".join(missing_t))

    conf = _think_clamp_conf(confidence)

    _ts = now_ts()
    tid = _new_id("t", _ts, text, "|".join(sorted(about_ids)))
    rec = {
        "id": tid,
        "ts": _ts,
        "about": about_ids,
        "topic": (topic or "").strip(),
        "thought": text,
        "based_on": based_ids,
        "confidence": conf,
    }
    append_jsonl(_think_path(store), rec)
    _think_index_put(store, tid, text)
    return tid


def think_timeline(store, topic):
    """同一 topic 的思考时间线，按 ts 升序。topic 为空则返回全部思考。"""
    key = (topic or "").strip().lower()
    rows = _think_iter(store)
    if key:
        rows = [r for r in rows if str(r.get("topic") or "").strip().lower() == key]
    rows.sort(key=_think_sort_key)
    return rows


def think_about(store, cold_id):
    """所有提到这条冷记忆的思考，按 ts 升序。"""
    cid = (cold_id or "").strip()
    if not cid:
        return []
    rows = []
    for rec in _think_iter(store):
        about = rec.get("about") or []
        if isinstance(about, str):
            about = _think_norm_ids(about)
        if cid in about:
            rows.append(rec)
    rows.sort(key=_think_sort_key)
    return rows


# ---------------------------------------------------------------
# 线索门控 cue_gate（纯规则，不调模型，确定性）
# ---------------------------------------------------------------

CUE_WORDS = (
    "那次", "上回", "上次", "之前", "记不记得", "记得吗", "你还记得",
    "继续那件事", "后来呢", "当时", "那天", "那一晚", "前段时间",
    "去年", "上个月", "以前", "当初", "我们说好", "说好的",
)

CHITCHAT_WORDS = (
    "好累", "累了", "困", "饿", "无聊", "还行", "随便", "嗯", "哦",
    "在吗", "早安", "晚安", "在干嘛",
)

DEICTIC_MARKERS = ("那次", "那个", "那件", "那家", "那位")

_CUE_SORTED = tuple(sorted(CUE_WORDS, key=lambda w: (-len(w), w)))
_CHITCHAT_SORTED = tuple(sorted(CHITCHAT_WORDS, key=lambda w: (-len(w), w)))

_STOP_CHARS = set(
    " \t\r\n，。！？、；：,.!?;:\"'“”‘’（）()《》【】[]…—-~·/\\|+*&^%$#@"
)
_LEFT_TAIL_STOP = set("的了着过是在和与就都也还又才更很最不没把被给对从向往")
_LEFT_WORD_STOP = {
    "我们", "你们", "他们", "她们", "它们", "咱们", "大家", "这个", "那个",
    "这些", "那些", "什么", "怎么", "就是", "所以", "但是", "因为", "如果",
    "还有", "然后", "已经", "刚才", "现在", "今天", "明天", "昨天",
}


def _cue_right_token(msg, start):
    end = start
    n = len(msg)
    while end < n and msg[end] not in _STOP_CHARS:
        end += 1
    return msg[start:end]


def _cue_left_token(msg, end):
    start = end
    while start > 0 and msg[start - 1] not in _STOP_CHARS:
        start -= 1
    return msg[start:end]


def _cue_deictic_cues(msg):
    """紧邻 那个/那次/那件/那家/那位 的具体指代 token（长度>=2）。"""
    out = []
    n = len(msg)
    for marker in DEICTIC_MARKERS:
        pos = 0
        while True:
            i = msg.find(marker, pos)
            if i < 0:
                break
            after = i + len(marker)
            pos = after
            right = _cue_right_token(msg, after)
            if len(right) >= 2:
                out.append(marker + right[:4])
            left = _cue_left_token(msg, i)
            if len(left) >= 2 and left not in _LEFT_WORD_STOP:
                if left[-1] not in _LEFT_TAIL_STOP:
                    out.append(left[-4:] + marker)
    return out


def cue_gate(message: str, recent: list = None) -> dict:
    """判断这条消息是否值得去召回旧记忆。返回 {allow, reason, cues}。

    除了"那次/上回/你还记得"这类显式线索，还要看上下文：
    一个短句接在某个话题后面（"数字生命"、"排骨汤"），本身就是线索。
    否则每换一个话题就得被 cue 门挡住一次。
    """
    msg = (message or "").strip()
    cues = []

    # 承接上文：短句本身不是寒暄，就不该用"没线索"把它挡死。
    # 门槛只防"在吗""哈哈哈"这种，不要求字面重合——换话题本来就是常有的事。
    if 2 <= len(msg) <= 20:
        # 只有"整句就是寒暄/笑声"才挡（在吗、晚安、哈哈哈）；
        # 带着实质内容的（今天好累、明天要早起）照常放行。
        stripped = msg
        for w in _CHITCHAT_SORTED:
            stripped = stripped.replace(w, "")
        stripped = "".join(ch for ch in stripped if ch not in _STOP_CHARS)
        # 笑声/叠字（哈哈哈、呜呜呜、啊啊啊）也算没有内容
        if len(set(stripped)) <= 1 and stripped:
            return {"allow": False, "reason": "laugh", "cues": []}
        if len(stripped) <= 1:
            hit = [w for w in _CHITCHAT_SORTED if w in msg]
            return {"allow": False,
                    "reason": "chitchat:" + (hit[0] if hit else msg[:4]), "cues": []}
        if True:
            joined = " ".join(str(x) for x in (recent or []) if x)
            if len(joined) >= 10:
                return {"allow": True, "reason": "context:接话", "cues": []}
            # 没有上文也要放行：短实词查询最可能是在指某件旧事
            return {"allow": True, "reason": "short:短实词", "cues": []}

    if msg:
        for word in _CUE_SORTED:
            if word in msg and word not in cues:
                cues.append(word)
        for frag in _cue_deictic_cues(msg):
            if frag not in cues:
                cues.append(frag)

    cues = cues[:_CUE_MAX]
    if cues:
        return {"allow": True, "reason": "cue:" + cues[0], "cues": cues}

    hit = None
    for word in _CHITCHAT_SORTED:
        if word in msg:
            hit = word
            break
    if hit:
        return {"allow": False, "reason": "chitchat:" + hit, "cues": []}

    return {"allow": False, "reason": "no_cue", "cues": []}


# ---------------------------------------------------------------
# 旧库补表（幂等）
# ---------------------------------------------------------------

def patch_init_schema(store=None):
    """给旧库补 thought_fts 表。store 可为 Store / Path / str / sqlite3.Connection。"""
    if isinstance(store, sqlite3.Connection):
        created = _think_ensure_table(store)
        store.commit()
        return created

    if store is None:
        for fn_name in ("get_store", "open_store", "load_store", "default_store"):
            fn = globals().get(fn_name)
            if callable(fn):
                try:
                    store = fn()
                    break
                except TypeError:
                    continue
        if store is None:
            root = os.environ.get("LTM_ROOT")
            if root:
                store = Store(Path(root))
        if store is None:
            raise LtmError("patch_init_schema 需要 store / 连接 / 路径参数")

    target = getattr(store, "idx", store)
    con = sqlite3.connect(str(target))
    try:
        created = _think_ensure_table(con)
        con.commit()
    finally:
        con.close()
    return created


# ---------------------------------------------------------------
# CLI 命令
# ---------------------------------------------------------------

def _ltm_pick_arg(args, *names, default=None):
    for name in names:
        if hasattr(args, name):
            value = getattr(args, name)
            if value is not None:
                return value
    return default


def _ltm_open_store(args):
    st = getattr(args, "store", None)
    if st is not None:
        return st
    for fn_name in ("get_store", "open_store", "load_store", "make_store"):
        fn = globals().get(fn_name)
        if callable(fn):
            try:
                return fn(args)
            except TypeError:
                try:
                    return fn()
                except TypeError:
                    continue
    root = getattr(args, "root", None) or os.environ.get("LTM_ROOT")
    if root:
        return Store(Path(root))
    return Store(Path.home() / ".ltm")


def cmd_think(store, args):
    action = str(_ltm_pick_arg(
        args, "think_action", "think_cmd", "action", "subcmd", "cmd", default=""
    ) or "").strip().lower()
    content = _ltm_pick_arg(args, "content", "text", "thought", "message", default="") or ""

    if not action:
        action = "add" if content else "timeline"

    if action in ("add", "a"):
        about = _think_norm_ids(_ltm_pick_arg(args, "about", "about_ids", default=None))
        based_on = _think_norm_ids(
            _ltm_pick_arg(args, "based_on", "based", "based_on_ids", default=None)
        )
        topic = _ltm_pick_arg(args, "topic", default="") or ""
        confidence = _ltm_pick_arg(args, "confidence", "conf", default=0.6)
        try:
            tid = think_add(
                store, content, about,
                topic=topic, based_on=based_on, confidence=confidence,
            )
        except ValueError as exc:
            print("错误: " + str(exc))
            return 2
        print(tid)
        return 0

    if action in ("timeline", "tl", "list", "ls"):
        topic = _ltm_pick_arg(args, "topic", default="") or ""
        rows = think_timeline(store, topic)
        if not rows:
            print("（没有思考记录）")
            return 0
        for rec in rows:
            try:
                iso = ts_to_iso(float(rec.get("ts") or 0.0))
            except Exception:
                iso = str(rec.get("ts"))
            print("%s\t%s\t[%s]\t(%.2f)\t%s" % (
                iso,
                rec.get("id", ""),
                rec.get("topic", "") or "-",
                float(rec.get("confidence") or 0.0),
                rec.get("thought", ""),
            ))
            about = _think_norm_ids(rec.get("about"))
            based = _think_norm_ids(rec.get("based_on"))
            if about or based:
                extra = []
                if about:
                    extra.append("about=" + ",".join(about))
                if based:
                    extra.append("based_on=" + ",".join(based))
                print("    " + "  ".join(extra))
        return 0

    print("未知的 think 子命令: " + action)
    return 2


def cmd_cue(store, args):
    message = _ltm_pick_arg(args, "message", "text", "content", "msg", default="") or ""
    recent = _ltm_pick_arg(args, "recent", "recent_messages", default=None)
    if isinstance(recent, str):
        recent = [recent]
    if not recent:
        # 插件把最近几条正文从 stdin 递进来，避免命令行转义地狱
        try:
            if not sys.stdin.isatty():
                data = sys.stdin.read()
                if data.strip():
                    recent = [ln for ln in data.splitlines() if ln.strip()]
        except Exception:
            recent = None
    result = cue_gate(message, recent=recent)
    print(json.dumps(result, ensure_ascii=False))
    return 0



import os

_EV_COLD_CACHE = {}


def _store_events(store):
    """事件文件路径（events.jsonl）。"""
    return store.root / "events.jsonl"


def _store_raw(store):
    """冷层目录。"""
    return store.raw_dir


def _store_conn(store):
    """打开 idx.db 连接（复用 Store.connect 的 schema 初始化）。"""
    return store.connect()


def _ensure_event_fts(con):
    """确保 event_fts 存在（幂等）；当前 sqlite 不支持 FTS5 时返回 False。"""
    try:
        con.execute(EVENT_FTS_SQL)
        return True
    except sqlite3.OperationalError:
        return False

def _event_index_put(store, mid, detail, summary):
    """把事件写进 event_fts。FTS5 不支持 UPSERT，统一先删后插。"""
    con = _open_db(store)
    try:
        if not _ensure_event_fts(con):
            return
        con.execute("DELETE FROM event_fts WHERE mid = ?", (mid,))
        con.execute(
            "INSERT INTO event_fts(mid, detail, summary) VALUES (?, ?, ?)",
            (mid, detail or "", summary or ""),
        )
        con.commit()
    finally:
        con.close()

def _load_events(store):
    """读取 events.jsonl，返回 {mid: rec}。"""
    out = {}
    for rec in iter_jsonl(_store_events(store)):
        if isinstance(rec, dict) and rec.get("id"):
            out[str(rec["id"])] = rec
    return out

def _save_events(store, records):
    """按 id 保序，原子重写整个 events.jsonl。"""
    _store_events(store).parent.mkdir(parents=True, exist_ok=True)
    tmp = _store_events(store).with_name(_store_events(store).name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for mid in sorted(records):
            f.write(json.dumps(records[mid], ensure_ascii=False) + "\n")
    os.replace(tmp, _store_events(store))

def _cold_record(store, rid):
    """按 id 在 raw/*.jsonl 中查冷层原话记录（进程内缓存；冷层永不改，可安全缓存）。"""
    if rid in _EV_COLD_CACHE:
        return _EV_COLD_CACHE[rid]
    rec = None
    if _store_raw(store).exists():
        for path in sorted(_store_raw(store).glob("*.jsonl"), reverse=True):
            for item in iter_jsonl(path):
                if isinstance(item, dict) and item.get("id") == rid:
                    rec = item
                    break
            if rec is not None:
                break
    _EV_COLD_CACHE[rid] = rec
    return rec

def _cold_text(rec):
    """从冷层记录里取出原话文本。"""
    if not isinstance(rec, dict):
        return ""
    for key in ("content", "text", "raw", "body"):
        val = rec.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""

def stage_for(rec, now=None):
    """计算事件当前所处阶段（纯函数，确定性，不读写文件、不调模型）。

    规则：
      1. rec["open_loop"] 为真 且 有 ongoing_until 且 now < ongoing_until -> "detail"
      2. age = now - rec["last_mentioned"]（天）；age < 3.0 -> "detail"
      3. age >= 14.0 -> "cold"（先判最远的）
      4. 有 summary 且 summary_source == "derived"
         且 (now - summary_updated_at) >= 24h -> "summary"
      5. 其余 -> "detail"（还没合格摘要，暂时继续用细节）
    """
    now = now_ts() if now is None else float(now)

    # 规则 1：未闭环且仍在进行中的事，一直走细节
    if rec.get("open_loop"):
        until = rec.get("ongoing_until")
        if until is not None:
            try:
                if now < float(until):
                    return STAGE_DETAIL
            except (TypeError, ValueError):
                pass

    # 规则 2：3 天内保留细节
    try:
        last = float(rec.get("last_mentioned"))
    except (TypeError, ValueError):
        try:
            last = float(rec.get("ts"))
        except (TypeError, ValueError):
            last = now
    age = (now - last) / 86400.0
    if age < DETAIL_AGE_DAYS:
        return STAGE_DETAIL

    # 规则 3：太久没被提及，直接沉到冷层（先判远的）
    if age >= COLD_AGE_DAYS:
        return STAGE_COLD

    # 规则 4：摘要合格且已稳定 24h 以上，才顶替细节
    summary = rec.get("summary")
    if summary and rec.get("summary_source") == "derived":
        try:
            updated = float(rec.get("summary_updated_at") or 0.0)
        except (TypeError, ValueError):
            updated = 0.0
        if (now - updated) >= SUMMARY_REFRESH_SEC:
            return STAGE_SUMMARY

    # 规则 5：还没摘要可用，暂留细节
    return STAGE_DETAIL

def event_add(store, sql, source_ids, topics=None, open_loop=False, importance=0.5, detail=None):
    """登记一条新事件，返回事件 id。

    sql: 事件内容串（CLI 的 <content>）。兼容旧用法：若传入 sqlite3.Connection，
         则视为数据库连接，内容全部由冷层原文提供。
    source_ids: 冷层原话 id 列表，至少一个。
    topics: 主题列表。
    open_loop: 是否为未闭环事项。
    importance: 重要度 0.0-1.0，越界自动裁剪。
    detail: 原文片段；省略时从 source_ids 对应的冷层原文取前 800 字符。
    """
    conn = sql if isinstance(sql, sqlite3.Connection) else None
    content = "" if conn is not None else (sql or "")

    ids = []
    for rid in (source_ids or []):
        rid = str(rid).strip()
        if rid and rid not in ids:
            ids.append(rid)
    if not ids:
        raise LtmError("event_add: source_ids 不能为空，至少需要一个冷层原话 id")

    # 事件时间取「这段对话的真实时间」，不是建事件的时钟：
    # 否则 32 天前那一晚会被当成"刚刚发生"，三档生命永远停在 detail。
    ts = now_ts()
    if ids:
        spans = []
        for rid in ids:
            r = _cold_record(store, rid)
            if isinstance(r, dict) and r.get("ts"):
                spans.append(float(r["ts"]))
        if spans:
            ts = max(spans)

    # detail：省略则从冷层原文拼接并截断到 800 字符
    if detail is None:
        chunks = []
        for rid in ids:
            txt = _cold_text(_cold_record(store, rid))
            if txt:
                chunks.append(txt)
        detail = "\n".join(chunks).strip()
        if not detail:
            # 冷层查不到原文时退回调用方给的内容串，保证 detail 不为空
            detail = (content or "").strip()
    else:
        detail = str(detail).strip()
    detail = detail[:DETAIL_MAX_CHARS]

    try:
        importance = float(importance)
    except (TypeError, ValueError):
        importance = 0.5
    importance = min(1.0, max(0.0, importance))

    clean_topics = []
    for t in (topics or []):
        t = str(t).strip()
        if t and t not in clean_topics:
            clean_topics.append(t)

    mid = _new_id("m", ts, detail, extra=",".join(ids))

    # 同内容同来源重复登记时直接返回已有 id（幂等）
    records = _load_events(store)
    if mid in records:
        return mid

    rec = {
        "id": mid,
        "ts": ts,
        "created_at": ts,
        "source_ids": ids,
        "topics": clean_topics,
        "open_loop": bool(open_loop),
        "importance": importance,
        "last_mentioned": ts,
        "mention_count": 1,
        "stage": STAGE_DETAIL,          # 落盘值，只由 advance_stages() 维护
        "detail": detail,
        "summary": None,
        "summary_source": "unavailable",
        "summary_updated_at": 0.0,
        "superseded_by": None,
    }

    append_jsonl(_store_events(store), rec)
    _event_index_put(store, mid, rec["detail"], rec["summary"])
    return mid

def event_attach_summary(store, mid, summary, source="derived"):
    """给事件挂摘要。

    summary 为空 或 source != "derived"，一律记为 summary_unavailable：
    summary 置 None、summary_source 置 "unavailable"，且不覆盖 detail。
    """
    mid = str(mid)
    text = (summary or "").strip()
    ok = bool(text) and (source == "derived")

    records = _load_events(store)
    rec = records.get(mid)
    if rec is None:
        raise LtmError("event_attach_summary: 事件不存在 %s" % mid)

    if ok:
        rec["summary"] = text
        rec["summary_source"] = "derived"
    else:
        rec["summary"] = None
        rec["summary_source"] = "unavailable"
    rec["summary_updated_at"] = now_ts()
    records[mid] = rec

    _save_events(store, records)
    _event_index_put(store, mid, rec.get("detail"), rec.get("summary"))
    return rec

def event_render(store, mid, now=None, reveal=False):
    """渲染事件为对外文本，返回 {"id","stage","text","note"}。

    detail  -> 原文片段
    summary -> 摘要，note="简化自原始对话"
    cold    -> 默认空 text（不外发），note="cold"
               reveal=True 时吐原文/摘要 —— 只在「用户给了明确线索」后调用，
               相当于"翻到档案再读"，不是主动外发。
    """
    mid = str(mid)
    records = _load_events(store)
    rec = records.get(mid)
    if rec is None:
        raise LtmError("event_render: 事件不存在 %s" % mid)

    now = now_ts() if now is None else float(now)
    stage = stage_for(rec, now)

    if stage == STAGE_SUMMARY:
        text = rec.get("summary") or ""
        note = "简化自原始对话"
    elif stage == STAGE_COLD:
        if reveal:
            # 用户给了线索才翻：优先摘要，没有摘要给原文（截断）
            text = rec.get("summary") or (rec.get("detail") or "")[:600]
            note = "cold·按线索翻出"
        else:
            text = ""
            note = "cold"
    else:
        text = rec.get("detail") or ""
        note = ""

    return {"id": mid, "stage": stage, "text": text, "note": note}

def advance_stages(store, now=None):
    """把 stage_for 的计算结果写回 events.jsonl —— 唯一让 stage 落盘的入口。

    按 id 保序重写整个文件。返回 {"moved": 变化条数, "details": {mid: 旧阶段}}。
    """
    now = now_ts() if now is None else float(now)
    records = _load_events(store)

    moved = {}
    for mid in sorted(records):
        rec = records[mid]
        new_stage = stage_for(rec, now)
        old_stage = rec.get("stage") or STAGE_DETAIL
        if new_stage != old_stage:
            rec["stage"] = new_stage
            records[mid] = rec
            moved[mid] = old_stage

    if records:
        _save_events(store, records)
    return {"moved": len(moved), "details": moved}

def event_search(store, query, k=10, now=None):
    """在 detail + summary 上做词面检索，返回 [{"id","stage","text","score","note"}]。

    cold 事件仍可被搜到：返回 summary（没有则 detail），note 标 "cold"。
    """
    now = now_ts() if now is None else float(now)
    try:
        k = max(1, int(k))
    except (TypeError, ValueError):
        k = 10

    records = _load_events(store)
    hits = []
    for mid in sorted(records):
        rec = records[mid]
        if rec.get("superseded_by"):
            # 已被取代的事件不参与检索
            continue

        detail = rec.get("detail") or ""
        summary = rec.get("summary") or ""
        score = lexical_score(query, (detail + "\n" + summary).strip())
        if score <= 0.0:
            continue

        stage = stage_for(rec, now)
        if stage == STAGE_SUMMARY:
            text = summary or detail
            note = "简化自原始对话"
        elif stage == STAGE_COLD:
            text = summary or detail
            note = "cold"
        else:
            text = detail
            note = ""

        hits.append({
            "id": mid,
            "stage": stage,
            "text": text,
            "score": score,
            "note": note,
        })

    hits.sort(key=lambda x: (-x["score"], x["id"]))
    return hits[:k]

def _split_csv(value):
    """把 "a,b,c" 拆成去空白、去重的列表。"""
    out = []
    for part in str(value or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def build_parser():
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="ltm", description="四层记忆系统 v2（第一部分）"
    )
    parser.add_argument("--root", default=DEFAULT_ROOT, help="数据根目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("remember", parents=[COMMON], help="记录一条原始对话到冷层")
    p.add_argument("content")
    p.add_argument("--role", default="user")
    p.add_argument("--session", default="default")
    p.add_argument("--ts", type=float, default=None, help="真实时间戳（补历史用，缺省=现在）")

    p = sub.add_parser("import", parents=[COMMON], help="从 JSONL 导入冷层记录")
    p.add_argument("src")
    p.add_argument("--session", default="default")

    p = sub.add_parser("fixed", parents=[COMMON], help="固定层操作")
    fixed_sub = p.add_subparsers(dest="fixed_cmd", required=True)
    fa = fixed_sub.add_parser("add", parents=[COMMON], help="新增固定层条目")
    fa.add_argument("--cat", required=True, choices=list(FIXED_CATS))
    fa.add_argument("content")
    fixed_sub.add_parser("list", parents=[COMMON], help="列出固定层条目")

    p = sub.add_parser("hot", parents=[COMMON], help="热层操作")
    hot_sub = p.add_subparsers(dest="hot_cmd", required=True)
    ha = hot_sub.add_parser("add", parents=[COMMON], help="新增热层条目")
    ha.add_argument("content")
    ha.add_argument("--from", dest="from_ids", required=True, help="冷层 id，逗号分隔")
    ha.add_argument("--topics", default="")
    ha.add_argument("--open-loop", action="store_true")

    p = sub.add_parser("reflect", parents=[COMMON], help="反射层操作（第二部分）")
    reflect_sub = p.add_subparsers(dest="reflect_cmd", required=True)
    ra = reflect_sub.add_parser("add", parents=[COMMON], help="新增反射层条目")
    ra.add_argument("content")
    ra.add_argument("--about", required=True, help="冷层 id，逗号分隔")
    ra.add_argument("--confidence", type=float, default=0.6)

    p = sub.add_parser("recall", parents=[COMMON], help="检索四层记忆（第二部分）")
    p.add_argument("query")
    p.add_argument("--budget", type=int, default=1200)
    p.add_argument("--k", type=int, default=10)
    p.add_argument("--layers", default="")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("project", parents=[COMMON], help="上下文投影（第二部分）")
    p.add_argument("query")
    p.add_argument("--budget", type=int, default=1200)
    p.add_argument("--k", type=int, default=10)

    p = sub.add_parser("consolidate", parents=[COMMON], help="后台整理（第二部分）")
    p.add_argument("--max-calls", type=int, default=20)
    p.add_argument("--max-tokens", type=int, default=20000)

    sub.add_parser("rebuild", parents=[COMMON], help="清空并重建 idx.db")

    p = sub.add_parser("verify", parents=[COMMON], help="校验文件与索引一致性")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("stats", parents=[COMMON], help="打印记忆库统计")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("state", parents=[COMMON], help="查看或修改 state.json")
    p.add_argument("--set", action="append", default=[], metavar="K=V")

    # ---- 第三部分：思考层 + 线索门控 ----
    p = sub.add_parser("think", parents=[COMMON], help="思考层操作")
    think_sub = p.add_subparsers(dest="think_cmd", required=True)
    ta = think_sub.add_parser("add", parents=[COMMON], help="新增一条思考")
    ta.add_argument("content")
    ta.add_argument("--about", required=True, help="冷层 id，逗号分隔")
    ta.add_argument("--topic", default="")
    ta.add_argument("--based-on", dest="based_on", default="", help="旧思考 id，逗号分隔")
    ta.add_argument("--confidence", type=float, default=0.6)
    tt = think_sub.add_parser("timeline", parents=[COMMON], help="按主题看思考时间线")
    tt.add_argument("--topic", required=True)

    p = sub.add_parser("cue", parents=[COMMON], help="判断这条消息该不该查冷库")
    p.add_argument("message")

    # ---- 第四部分：事件层（三档生命）----
    p = sub.add_parser("event", parents=[COMMON], help="事件层操作（detail/summary/cold）")
    ev_sub = p.add_subparsers(dest="event_cmd", required=True)
    ea = ev_sub.add_parser("add", parents=[COMMON], help="新增事件")
    ea.add_argument("content", nargs="?", default="")
    ea.add_argument("--from", dest="from_ids", required=True, help="冷层 id，逗号分隔")
    ea.add_argument("--topics", default="")
    ea.add_argument("--open-loop", dest="open_loop", action="store_true")
    es = ev_sub.add_parser("summary", parents=[COMMON], help="给事件挂摘要")
    es.add_argument("mid")
    es.add_argument("--text", required=True)
    esh = ev_sub.add_parser("show", parents=[COMMON], help="看一条事件的三种表示")
    esh.add_argument("mid")
    esh.add_argument("--reveal", action="store_true", help="cold 也吐正文（用户给了线索时用）")
    ev_sub.add_parser("advance", parents=[COMMON], help="推进阶段并落盘")
    esr = ev_sub.add_parser("search", parents=[COMMON], help="检索事件")
    esr.add_argument("query")
    esr.add_argument("--k", type=int, default=10)

    return parser



def cmd_event(store, args):
    """event 子命令分发。"""
    import json as _json
    act = getattr(args, "event_cmd", "")
    if act == "add":
        ids = [s.strip() for s in (args.from_ids or "").split(",") if s.strip()]
        topics = [s.strip() for s in (args.topics or "").split(",") if s.strip()]
        mid = event_add(store, args.content, ids, topics=topics, open_loop=args.open_loop)
        print(mid)
        return 0
    if act == "summary":
        event_attach_summary(store, args.mid, args.text)
        print("摘要已挂上：" + args.mid)
        return 0
    if act == "show":
        r = event_render(store, args.mid, reveal=bool(getattr(args, "reveal", False)))
        print(_json.dumps(r, ensure_ascii=False, indent=1))
        return 0
    if act == "advance":
        r = advance_stages(store)
        print(_json.dumps(r, ensure_ascii=False))
        return 0
    if act == "search":
        rows = event_search(store, args.query, k=args.k)
        for it in rows:
            print("%.4f\t%s\t%s\t%s" % (it["score"], it["stage"], it["id"], (it["text"] or "")[:120]))
        return 0
    raise LtmError("未知的 event 子命令：" + str(act))



HANDLERS = {
    "remember": cmd_remember,
    "import": cmd_import,
    "fixed": cmd_fixed,
    "hot": cmd_hot,
    "reflect": cmd_reflect,
    "recall": cmd_recall,
    "project": cmd_project,
    "consolidate": cmd_consolidate,
    "rebuild": cmd_rebuild,
    "verify": cmd_verify,
    "stats": cmd_stats,
    "state": cmd_state,
    "think": cmd_think,
    "cue": cmd_cue,
    "event": cmd_event,
}


def main(argv=None):
    """CLI 入口：解析参数、分发命令、统一错误处理。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    store = Store(args.root)
    try:
        store.ensure()
        handler = HANDLERS.get(args.cmd)
        if handler is None:
            raise LtmError("未知命令：{0}".format(args.cmd))
        return handler(store, args) or 0
    except LtmError as exc:
        print("错误：{0}".format(exc), file=sys.stderr)
        return 1
    except NotImplementedError as exc:
        print("未实现（留待第二部分）：{0}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


STAGE_DETAIL = "detail"
STAGE_SUMMARY = "summary"
STAGE_COLD = "cold"
DETAIL_AGE_DAYS = 3.0
COLD_AGE_DAYS = 14.0
SUMMARY_REFRESH_SEC = 24 * 3600
DETAIL_MAX_CHARS = 800


EVENT_FTS_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS event_fts "
    "USING fts5(mid UNINDEXED, detail, summary)"
)

if __name__ == "__main__":
    sys.exit(main())
