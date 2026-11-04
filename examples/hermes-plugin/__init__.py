"""Hermes 记忆插件：ltm-memory。

把 ltm2.py 的七层记忆接到 Hermes 的 MemoryProvider 钩子上：
  - prefetch  每轮开始前按线索召回
  - sync_turn 每轮结束后写库（写入前脱敏）

注意：本插件与 dsh-memory 功能重叠，**不能同时开启**，
否则会出现重复写入与重复召回。

后端 CLI：
    python3 ltm2.py --root <ROOT> remember "<内容>" --role user|assistant --session <SID>
    python3 ltm2.py --root <ROOT> recall "<查询>" --k 5
    python3 ltm2.py --root <ROOT> cue "<消息>"

环境变量：
    LTM_ROOT   记忆库根目录（缺省 ~/.ltm）
    LTM_CLI    后端 CLI 路径（缺省 $LTM_ROOT/ltm2.py）
"""

import json
import logging
import os
import pathlib as pathlib_mod
import queue
import subprocess
import threading
import time

try:
    from agent.memory_provider import MemoryProvider
except Exception:  # 允许在无 Hermes 运行时的情况下单独导入本文件

    class MemoryProvider(object):  # type: ignore[no-redef]
        name = ""

        def is_available(self):
            return False

        def unavailable_reason(self):
            return "agent.memory_provider 不可用"

        def initialize(self, session_id, **kwargs):
            return None

        def system_prompt_block(self):
            return ""

        def prefetch(self, query, *, session_id=""):
            return ""

        def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
            return None

        def on_session_end(self, messages):
            return None

        def shutdown(self):
            return None


logger = logging.getLogger(__name__)

# 可选增强：向量近邻 + 摘要脱敏。缺失/报错都不影响主链路。
try:
    from . import ltm_embed
except Exception:  # 单文件加载（测试）时的退路
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "ltm_embed", os.path.join(os.path.dirname(os.path.abspath(__file__)), "ltm_embed.py"))
        ltm_embed = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(ltm_embed)
    except Exception:
        ltm_embed = None

__all__ = ["LtmMemoryProvider", "register"]

PYTHON_BIN = "python3"

DEFAULT_ROOT = os.environ.get("LTM_ROOT") or str(pathlib_mod.Path.home() / ".ltm")
DEFAULT_CLI = os.environ.get("LTM_CLI") or str(pathlib_mod.Path.home() / ".ltm" / "ltm2.py")

CUE_TIMEOUT = 8.0
RECALL_TIMEOUT = 12.0     # 向量自己 3 秒就放弃，这里留余量给词法
REMEMBER_TIMEOUT = 15.0
RECALL_K = 5

DRAIN_TIMEOUT = 5.0
POLL_INTERVAL = 0.05

UNAVAILABLE_TEXT = "（记忆暂时不可用）"
SYSTEM_BLOCK_TEXT = "本地长期记忆已连接。需要查旧事时用 recall 工具。"


class LtmMemoryProvider(MemoryProvider):
    name = "ltm-memory"

    def __init__(self, root=None, cli=None):
        self._root = os.path.abspath(
            os.path.expanduser(root or os.environ.get("LTM_ROOT") or DEFAULT_ROOT)
        )
        self._cli = os.path.abspath(
            os.path.expanduser(cli or os.environ.get("LTM_CLI") or DEFAULT_CLI)
        )
        self._session_id = ""
        self._recent = []          # 最近几轮说的话，给 cue 判断"是不是在接上文"
        self._recent_lock = threading.Lock()
        self._queue = queue.Queue()
        self._lock = threading.Lock()
        self._pending = 0
        self._worker = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ 元信息

    def is_available(self):
        try:
            return True
        except Exception:
            logger.warning("ltm-memory: is_available 失败", exc_info=True)
            return False

    def unavailable_reason(self):
        try:
            return ""
        except Exception:
            logger.warning("ltm-memory: unavailable_reason 失败", exc_info=True)
            return ""

    def initialize(self, session_id, **kwargs):
        try:
            self._session_id = session_id or ""
            os.makedirs(self._root, exist_ok=True)
            self._ensure_worker()
        except Exception:
            logger.warning("ltm-memory: initialize 失败", exc_info=True)

    # ------------------------------------------------------------------ 工具
    # 插件不提供额外工具：召回走 prefetch，写入走 sync_turn。
    # 但 ABC 要求这两个方法必须存在，缺了会直接实例化失败。

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return ""

    def get_config_schema(self):
        return []

    def save_config(self, values, hermes_home):
        return None

    def system_prompt_block(self):
        try:
            return SYSTEM_BLOCK_TEXT
        except Exception:
            logger.warning("ltm-memory: system_prompt_block 失败", exc_info=True)
            return ""

    # ------------------------------------------------------------------ 召回

    def prefetch(self, query, *, session_id=""):
        try:
            q = (query or "").strip()
            if not q:
                return ""

            # 1) cue gate：不允许召回就直接返回空
            recent = self._recent_lines()
            if not self._cue_gate(q, recent):
                return ""

            # 2) recall
            code, out, err = self._run_cli(["recall", q, "--k", str(RECALL_K)], RECALL_TIMEOUT)
            if code != 0:
                logger.warning(
                    "ltm-memory: recall 返回码 %s: %s", code, (err or "").strip()[:200]
                )
                return UNAVAILABLE_TEXT
            try:
                data = json.loads(out)
            except Exception:
                logger.warning("ltm-memory: recall 输出不是合法 JSON")
                return UNAVAILABLE_TEXT

            # 3) 词法不够用时，向量补一刀（可选，失败静默）
            data = self._augment_with_vectors(q, data)
            return self._format_recall(data)
        except subprocess.TimeoutExpired:
            logger.warning("ltm-memory: prefetch 超时")
            return UNAVAILABLE_TEXT
        except Exception:
            logger.warning("ltm-memory: prefetch 失败", exc_info=True)
            return UNAVAILABLE_TEXT

    def _augment_with_vectors(self, query, data):
        """词法最高分不够看时，用向量近邻补候选。

        判据是"最高分"而不是"条数"：一堆撞了一两个字的低分候选
        中看不中用，那种局面正是语义该出手的时候。
        任何异常都原样返回 data —— 向量挂了记忆库也得好用。
        """
        if ltm_embed is None or not isinstance(data, dict):
            return data
        try:
            pack = data.get("pack") or []
            top = 0.0
            for it in pack:
                try:
                    top = max(top, float(it.get("score") or 0))
                except (TypeError, ValueError):
                    continue
            if top >= ltm_embed.LEXICAL_STRONG:
                return data

            hits = ltm_embed.vector_hits(os.path.join(self._root, "idx.db"), query)
            if not hits:
                return data

            have = {str(it.get("id")) for it in pack if isinstance(it, dict)}
            news = [{"id": k, "score": v} for k, v in hits.items() if k not in have]
            if not news:
                return data

            # 按 id 取出正文：先走 event show，再退回冷层 CLI
            extra = []
            for item in news[:8]:
                mid = item["id"]
                code, out, _ = self._run_cli(["event", "show", mid, "--reveal"], RECALL_TIMEOUT)
                if code == 0 and out:
                    try:
                        r = json.loads(out)
                        text = (r.get("text") or "").strip()
                        if text:
                            extra.append({
                                "id": mid, "layer": "event", "content": text,
                                "note": r.get("note") or "", "score": item["score"],
                                "vec_score": round(float(item["score"]), 4),
                            })
                    except Exception:
                        pass
            if extra:
                data = dict(data)
                data["pack"] = extra + pack
        except Exception:
            logger.debug("ltm-memory: 向量增强失败，忽略", exc_info=True)
        return data

    def _recent_lines(self):
        """取最近几轮要说的话（给 cue 判断"是不是在接上文"用）。"""
        try:
            with self._recent_lock:
                msgs = list(self._recent)
        except Exception:
            return []
        out = []
        for m in msgs[-4:]:
            if isinstance(m, dict):
                c = m.get("content") or ""
            else:
                c = str(m)
            c = " ".join(str(c).split())
            if c:
                out.append(c[:200])
        return out

    def _cue_gate(self, message, recent=None):
        stdin_text = "\n".join(str(x) for x in (recent or []) if x) or None
        code, out, err = self._run_cli(["cue", message], CUE_TIMEOUT, stdin_text=stdin_text)
        if code != 0:
            raise RuntimeError("cue 返回码 %s: %s" % (code, (err or "").strip()[:200]))
        data = json.loads(out)
        if not isinstance(data, dict):
            raise ValueError("cue 输出不是 JSON 对象")
        return bool(data.get("allow"))

    def _format_recall(self, data):
        """把召回结果写成"我此刻想起来的几件事"，不是数据库报表。

        两条形态差异：
          - 近的（detail）：带原文，直接说
          - 远的（cold / 久远的 summary）：写清"多久以前"，不冒充刚发生
        并且 cold 的正文要单独翻开（cue 已放行 = 用户给了线索）。
        """
        if not isinstance(data, dict):
            return UNAVAILABLE_TEXT
        pack = data.get("pack") or []
        if not isinstance(pack, list) or not pack:
            return ""

        now = time.time()
        blocks = []

        for item in pack:
            if not isinstance(item, dict):
                continue
            content = (item.get("content") or "").strip()
            layer = item.get("layer") or "?"
            mem_id = item.get("id") or ""
            note = (item.get("note") or "").strip()
            ts = item.get("ts")

            # cold 且没正文 → 用户给了线索，翻开它
            if not content and (note.startswith("cold") or layer == "cold") and mem_id:
                code, out, err = self._run_cli(
                    ["event", "show", mem_id, "--reveal"], RECALL_TIMEOUT)
                if code == 0 and out:
                    try:
                        r = json.loads(out)
                        content = (r.get("text") or "").strip()
                        note = (r.get("note") or note).strip()
                    except Exception:
                        pass

            if not content:
                continue

            # 时间感：距今多久
            age = ""
            try:
                if ts:
                    d = (now - float(ts)) / 86400.0
                    if d >= 1:
                        age = "%d 天前" % int(d)
                    else:
                        age = "%d 小时前" % max(1, int(d * 24))
            except (TypeError, ValueError):
                age = ""

            head = "· "
            if age:
                head += "（%s）" % age
            if layer == "event" or note.startswith("cold"):
                head += " "
            blocks.append((head, content, note))

        if not blocks:
            return ""

        lines = ["### 想起来的几件事（很久以前的事标了时间，细节可能记岔，别当铁证）"]
        for head, content, note in blocks:
            body = content.replace("\n", " ").strip()
            if len(body) > 400:
                body = body[:400] + "…"
            tail = ""
            if note and "cold" in note:
                tail = "（这是很久以前的事）"
            lines.append(head + body + tail)
        return "\n".join(lines)

    # ------------------------------------------------------------------ 写入

    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None, turn_author=None):
        try:
            logger.warning("ltm-memory: sync_turn 被调用 uid=%s aid=%s",
                           len(user_content or ""), len(assistant_content or ""))
            user_text = user_content if isinstance(user_content, str) else ""
            assistant_text = assistant_content if isinstance(assistant_content, str) else ""
            if not user_text.strip() and not assistant_text.strip():
                return

            sid = session_id or self._session_id
            self._ensure_worker()

            # 留一份上文给 cue 用（"数字生命"这种短句本身不显眼，靠上文才成线索）
            with self._recent_lock:
                if user_text.strip():
                    self._recent.append({"role": "user", "content": user_text[:300]})
                if assistant_text.strip():
                    self._recent.append({"role": "assistant", "content": assistant_text[:300]})
                del self._recent[:-8]

            # 写入前脱敏：摘要/加工文本会进上下文，凭证不能留在这层。
            # 冷层原文另有一份（后端 raw/），不受影响。
            if ltm_embed is not None:
                user_text = ltm_embed.scrub(user_text)
                assistant_text = ltm_embed.scrub(assistant_text)

            if user_text.strip():
                self._enqueue(user_text, "user", sid)
            if assistant_text.strip():
                self._enqueue(assistant_text, "assistant", sid)
        except Exception:
            logger.warning("ltm-memory: sync_turn 失败", exc_info=True)

    def _enqueue(self, content, role, session_id):
        with self._lock:
            self._pending += 1
        try:
            self._queue.put((content, role, session_id))
        except Exception:
            self._mark_done()
            raise

        worker = self._worker
        if worker is None or not worker.is_alive():
            self._ensure_worker()

    def _remember(self, content, role, session_id):
        args = ["remember", content, "--role", role]
        if session_id:
            args.extend(["--session", session_id])
        code, out, err = self._run_cli(args, REMEMBER_TIMEOUT)
        if code != 0:
            logger.warning(
                "ltm-memory: remember(%s) 返回码 %s: %s",
                role,
                code,
                (err or "").strip()[:200],
            )

    # ------------------------------------------------------------------ 后台线程

    def _ensure_worker(self):
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop.clear()
            worker = threading.Thread(
                target=self._worker_loop, name="ltm-memory-writer", daemon=True
            )
            self._worker = worker
            worker.start()

    def _worker_loop(self):
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                if self._stop.is_set() and self._queue.empty():
                    return
                time.sleep(POLL_INTERVAL)
                continue

            try:
                if item is not None:
                    content, role, session_id = item
                    self._remember(content, role, session_id)
            except Exception:
                logger.warning("ltm-memory: 后台写入失败", exc_info=True)
            finally:
                self._mark_done()

    def _mark_done(self):
        try:
            with self._lock:
                if self._pending > 0:
                    self._pending -= 1
        except Exception:
            logger.warning("ltm-memory: 计数失败", exc_info=True)

    def _drain(self, timeout=DRAIN_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                pending = self._pending
            if pending <= 0 and self._queue.empty():
                return
            time.sleep(POLL_INTERVAL)

    # ------------------------------------------------------------------ 生命周期

    def on_session_end(self, messages):
        try:
            self._drain(DRAIN_TIMEOUT)
        except Exception:
            logger.warning("ltm-memory: on_session_end 失败", exc_info=True)

    def shutdown(self):
        try:
            self._stop.set()
            self._drain(DRAIN_TIMEOUT)
            worker = self._worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=DRAIN_TIMEOUT)
            self._worker = None
        except Exception:
            logger.warning("ltm-memory: shutdown 失败", exc_info=True)

    # ------------------------------------------------------------------ CLI 调用

    def _run_cli(self, args, timeout, stdin_text=None):
        cmd = [PYTHON_BIN, self._cli, "--root", self._root] + [str(a) for a in args]
        env = dict(os.environ)
        env["no_proxy"] = "*"

        proc = subprocess.run(
            cmd,
            input=(stdin_text or "").encode("utf-8") if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
            check=False,
        )
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        return proc.returncode, out, err


def register(ctx):
    """Hermes 插件约定入口。

    注意：Hermes 只认 ctx.register_memory_provider(实例) 这条路，
    return 一个实例是没用的（日志会说 "loaded but no provider instance found"）。
    """
    ctx.register_memory_provider(LtmMemoryProvider())
