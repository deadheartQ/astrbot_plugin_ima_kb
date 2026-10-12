"""astrbot_plugin_ima_kb —— 让机器人查腾讯 ima 知识库（含订阅库）。

原理：用浏览器登录态（x-ima-cookie / x-ima-bkn）调 ima 内部接口
`init_session` → `qa`(SSE)，把答案洗成纯文本后交给模型**转述**。

为什么不走 OpenAPI：`/openapi/wiki/v1/` 按知识库归属鉴权，订阅库会返回 220030 无权限；
内部接口的权限跟随账号登录态，订阅库可以正常读取。
"""

from __future__ import annotations

import asyncio
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .core.cleaner import clean
from .core.ima_client import ImaAuthError, ImaClient, ImaError


def _pick(cfg: dict, path: str, default=None):
    """按点路径取配置项。

    AstrBot 的分组（`type: object` + `items`）会让配置变成**嵌套**结构，
    所以这里同时兼容 `auth.x_ima_cookie` 与扁平的 `x_ima_cookie` 两种写法。
    """
    if not isinstance(cfg, dict):
        return default
    if path in cfg:
        return cfg[path]
    cur: object = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def parse_knowledge_bases(raw) -> dict[str, str]:
    """解析知识库配置，返回「名字 → 库ID」。

    支持两种形态（list / 多行文本都行），每一条支持两种写法：

    - ``名字=库ID``  ← 推荐。插件才能按题材（如"海龟汤"）选对库
    - ``库ID``       ← 只填 ID 也认，名字用 ID 兜底；不会报错，
                        但插件无法区分库的题材，只会用列表里的第一个

    只填 ID 时不视为"未配置"——这是刻意的：缺个名字不该让整个工具罢工。
    """
    if isinstance(raw, str):
        lines = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        lines = [str(x) for x in raw]
    else:
        lines = []
    out: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            name, _, kid = line.partition("=")
            name, kid = name.strip(), kid.strip()
        else:
            # 只有 ID：名字兜底用 ID 本身
            name = kid = line
        if name and kid:
            out[name] = kid
    return out


def build_question(question: str, template: str = "", limit: int = 0) -> str:
    """把用户问题包成**真正发给 ima 的提示词**。

    这是"控制 ima 生成答案"的唯一入口（ima 侧只认提示词，没有长度参数）：

    - ``template`` 为空 ⇒ 原样发送用户问题（默认行为，等于没这个功能）
    - ``template`` 含 ``{question}`` ⇒ 就地替换（推荐，指令可以写在前后）
    - ``template`` 不含占位符 ⇒ 当成前置指令，问题**附在下一行**
    - ``limit`` > 0 ⇒ 末尾追加一句字数限制。写死数字是为了让模板保持简单：
      想改长度只调 limit，想改风格才动 template。
    """
    q = (question or "").strip()
    if not q:
        return ""
    tpl = (template or "").strip()
    if not tpl:
        out = q
    elif "{question}" in tpl:
        out = tpl.replace("{question}", q).strip()
    else:
        out = f"{tpl}\n{q}"
    if limit > 0:
        out = f"{out}\n（请用不超过 {limit} 字回答，直接给结论和要点，不要复述原文。）"
    return out


@register(
    "astrbot_plugin_ima_kb",
    "Buddy",
    "让机器人查询腾讯 ima 知识库：游戏设定、角色资料、剧情考据等专业内容，支持订阅库",
    "1.1.0",
)
class ImaKbPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        self.cfg = cfg

        self.cookie = str(_pick(cfg, "auth.x_ima_cookie", "") or "").strip()
        self.bkn = str(_pick(cfg, "auth.x_ima_bkn", "") or "").strip()
        self.kbs = parse_knowledge_bases(_pick(cfg, "knowledge.knowledge_bases", []))
        self.default_kb = str(
            _pick(cfg, "knowledge.default_knowledge_base", "") or ""
        ).strip()
        self.max_seconds = int(_pick(cfg, "runtime.max_seconds", 45) or 45)
        self.max_chars = int(_pick(cfg, "runtime.max_chars", 900) or 900)
        self.cache_minutes = int(_pick(cfg, "runtime.cache_minutes", 10) or 0)
        self.enable_tool = bool(_pick(cfg, "runtime.enable_llm_tool", True))
        # 提问提示词：ima 侧没有"长度参数"，只能靠提示词约束答案篇幅
        self.ask_prompt = str(_pick(cfg, "runtime.ask_prompt", "") or "").strip()
        self.answer_limit = max(0, int(_pick(cfg, "runtime.answer_limit", 0) or 0))

        self._client: ImaClient | None = None
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._sem = asyncio.Semaphore(1)  # ima 侧并发敏感（Code=3），串行更稳

        if not self.cookie or not self.bkn:
            logger.warning("[ima] 未配置 x-ima-cookie / x-ima-bkn，知识库工具不可用")
        elif not self.kbs:
            logger.warning("[ima] 未配置任何知识库（knowledge_bases 为空）")
        else:
            logger.info(f"[ima] 已配置 {len(self.kbs)} 个知识库: {list(self.kbs)}")
            if self.ask_prompt or self.answer_limit > 0:
                logger.info(f"[ima] 提问提示词已启用：{self._prompt_desc()}")
            if all(name == kid for name, kid in self.kbs.items()):
                logger.warning(
                    "[ima] 知识库只填了 ID、没有名字，插件无法按题材选库，"
                    "会一律查列表第一个；建议改成「名字=库ID」"
                )

    # ---------------------------------------------------------------- 内部

    def _prompt_desc(self) -> str:
        """提问提示词的人话描述（启动日志与 /ima_status 共用）。"""
        if not self.ask_prompt and self.answer_limit <= 0:
            return "未配置（原样提问）"
        bits = []
        if self.ask_prompt:
            bits.append(f"模板 {len(self.ask_prompt)} 字")
        if self.answer_limit > 0:
            bits.append(f"答案上限 {self.answer_limit} 字")
        return " + ".join(bits)

    def _ask_text(self, question: str) -> str:
        """把问题包成真正发给 ima 的提示词（空结果兜底回原问题）。"""
        return build_question(question, self.ask_prompt, self.answer_limit) or question

    def _get_client(self) -> ImaClient:
        if self._client is None:
            self._client = ImaClient(
                self.cookie, self.bkn, timeout=self.max_seconds + 15
            )
        return self._client

    _KB_SPLIT = re.compile(r"[\s·|/、，,：:（）()\[\]【】]+")
    # 库里名字常带修饰词，用户只会说其中一小截（「终末地」「海龟汤」），
    # 所以允许用「长度 >= 3 的连续子串」去命中；2 字太容易误伤（如「明日」）
    _KB_MIN_PARTIAL = 3

    @classmethod
    def _kb_score(cls, name: str, question: str) -> float:
        """库名与问题的匹配分，0 表示不相关、越大越像。

        打分规则：
        - 库名按分隔符切出的片段完整出现在问题里 → 1000 起（强命中），
          并按「命中片段占库名长度的比例」做微调 —— 这样问「明日方舟」时
          会选短库名「明日方舟」，而不是「明日方舟·终末地档案」。
        - 否则退到片段内长度 >= 3 的连续子串，取最长命中长度。
        """
        if not name or not question:
            return 0.0
        best = 0.0
        ln = len(name)
        for token in cls._KB_SPLIT.split(name):
            token = token.strip()
            if len(token) < 2:
                continue
            if token in question:
                best = max(best, 1000 + len(token) * 10 + len(token) / ln * 5)
                continue
            for size in range(len(token) - 1, cls._KB_MIN_PARTIAL - 1, -1):
                if any(
                    token[i : i + size] in question
                    for i in range(len(token) - size + 1)
                ):
                    best = max(best, size + size / ln * 5)
                    break
        return best

    def _pick_kb(self, question: str, want: str = "") -> tuple[str, str]:
        """返回 (库名, 库ID)。

        顺序：显式指定 → 库名/问题的匹配分 → 默认库 → 列表第一个。
        """
        w = (want or "").strip()
        if w:
            for name, kid in self.kbs.items():  # 精确
                if w == name or w == kid:
                    return name, kid
            for name, kid in self.kbs.items():  # 互相包含（只写了一半）
                if w in name or name in w:
                    return name, kid

        if len(self.kbs) == 1:
            name = next(iter(self.kbs))
            return name, self.kbs[name]

        best_score, best_name, best_id = 0.0, "", ""
        for name, kid in self.kbs.items():
            s = self._kb_score(name, question)
            if s > best_score:
                best_score, best_name, best_id = s, name, kid
        if best_score > 0:
            return best_name, best_id

        if self.default_kb:
            for name, kid in self.kbs.items():
                if name == self.default_kb or kid == self.default_kb:
                    return name, kid
        name = next(iter(self.kbs))
        return name, self.kbs[name]

    def _cache_get(self, kb_id: str, question: str) -> str | None:
        if self.cache_minutes <= 0:
            return None
        hit = self._cache.get((kb_id, question))
        if not hit:
            return None
        text, ts = hit
        if time.time() - ts > self.cache_minutes * 60:
            self._cache.pop((kb_id, question), None)
            return None
        return text

    def _cache_put(self, kb_id: str, question: str, text: str) -> None:
        if self.cache_minutes <= 0:
            return
        if len(self._cache) > 200:
            self._cache.clear()
        self._cache[(kb_id, question)] = (text, time.time())

    # ---------------------------------------------------------------- 工具

    @filter.llm_tool(name="ima_ask")
    async def ima_ask(
        self, event: AstrMessageEvent, question: str, knowledge_base: str = ""
    ):
        """查询 ima 知识库（腾讯 AI 知识管家）里的专业资料。

        当用户问到游戏设定、角色档案、剧情考据、道具机制等"需要查资料"的内容时调用，
        尤其适合《明日方舟》《明日方舟：终末地》《烧脑海龟汤》这类收录在知识库里的题材。
        闲聊、日常对话、你已经知道答案的问题，不要调用。

        Args:
            question(string): 要查询的问题，写成完整、具体的一句，例如「管理员是什么角色」
            knowledge_base(string): 可选。指定知识库名称；不确定就留空，会自动选
        """
        if not args_ok(self.enable_tool, self.cookie, self.bkn, self.kbs):
            return "[ima] 知识库工具未配置或已关闭，无法查询。请当作没有这条资料，正常回答用户。"

        kb_name, kb_id = self._pick_kb(question, knowledge_base)
        # 选库用**用户原话**（模板里可能带与库名无关的指令，会干扰匹配），
        # 缓存键用**实际发出去的提示词**（改了提示词/字数上限后旧答案自动失效）
        ask_q = self._ask_text(question)
        cached = self._cache_get(kb_id, ask_q)
        if cached:
            logger.info(f"[ima] 命中缓存：{kb_name} / {question[:30]}")
            return self._wrap(kb_name, cached)

        t0 = time.time()
        try:
            async with self._sem:  # 串行，避免 ima 侧 Code=3
                raw = await self._get_client().ask(
                    ask_q, kb_id, max_seconds=self.max_seconds
                )
            cost = time.time() - t0
            text = clean(raw, max_len=self.max_chars)
            if not text:
                logger.warning(f"[ima] {kb_name} 返回空内容（{cost:.1f}s）")
                return "[ima] 知识库里没有相关内容。请按你原本的理解正常回答用户，不要提这条。"
            logger.info(f"[ima] {kb_name} 查询完成：{cost:.1f}s，{len(text)} 字")
            self._cache_put(kb_id, ask_q, text)
            return self._wrap(kb_name, text)
        except ImaAuthError as e:
            logger.error(f"[ima] 登录态失效：{e}")
            return "[ima] 知识库登录态失效，需要管理员重新配置。请正常回答用户，不要提这条。"
        except ImaError as e:
            logger.warning(f"[ima] 查询失败（{time.time() - t0:.1f}s）：{e}")
            return "[ima] 本次查询失败。请按你原本的理解回答用户，不要提这条。"
        except Exception as e:  # noqa: BLE001
            logger.error(f"[ima] 未预期异常：{type(e).__name__}: {e}", exc_info=True)
            return "[ima] 查询出错。请按你原本的理解回答用户，不要提这条。"

    def _wrap(self, kb_name: str, text: str) -> str:
        """包装成"资料 + 转述要求"，这是让模型别照抄的关键。"""
        return (
            f"【ima 知识库·{kb_name}】以下是你查到的资料。\n"
            f"要求：用你自己平时的说话方式讲给用户听，可以精简、可以只挑重点，"
            f"但不要照抄原文，也不要出现'知识库''资料''根据查询'这类字眼。\n"
            f"----\n{text}"
        )

    # ---------------------------------------------------------------- 命令

    @filter.command("ima")
    async def ima_cmd(self, event: AstrMessageEvent):
        """手动查一次知识库：/ima 你的问题"""
        parts = event.message_str.split(" ", 1)
        q = parts[1].strip() if len(parts) > 1 else ""
        if not q:
            names = "、".join(self.kbs) or "（未配置）"
            yield event.plain_result(f"用法：/ima 你的问题\n可用知识库：{names}")
            return
        kb_name, kb_id = self._pick_kb(q)
        ask_q = self._ask_text(q)
        try:
            async with self._sem:
                raw = await self._get_client().ask(
                    ask_q, kb_id, max_seconds=self.max_seconds
                )
            text = clean(raw, max_len=0)
            yield event.plain_result(f"【{kb_name}】\n{text or '（没查到内容）'}")
        except (ImaError, ImaAuthError) as e:
            yield event.plain_result(f"查询失败：{e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("ima_status")
    async def ima_status(self, event: AstrMessageEvent):
        """看知识库工具的状态。"""
        lines = [
            "ima 知识库插件状态",
            f"cookie/bkn：{'已配置' if self.cookie and self.bkn else '缺失'}",
            f"知识库：{len(self.kbs)} 个 {list(self.kbs) if self.kbs else ''}",
            f"提问提示词：{self._prompt_desc()}",
            f"超时：{self.max_seconds}s   截断：{self.max_chars} 字   缓存：{self.cache_minutes} 分钟",
            f"缓存条目：{len(self._cache)}",
        ]
        yield event.plain_result("\n".join(lines))


def args_ok(enabled: bool, cookie: str, bkn: str, kbs: dict) -> bool:
    return bool(enabled and cookie and bkn and kbs)
