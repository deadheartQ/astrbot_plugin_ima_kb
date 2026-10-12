"""腾讯 ima 内部接口客户端（cookie 认证）。

协议来自 highkay/tencent-ima-copilot-mcp 的实测实现（MIT），这里做了瘦身与加固：

- 只保留三个接口：init_session（会话绑定知识库）/ qa（SSE 问答）/ refresh（续期）
- SSE 按行解析，只取 `Data.text_message.Text` 片段拼接；错误帧（toast_task）单独收集
- **session 缓存**：同一个知识库复用 session_id，省掉每次 1~2 秒的 init 往返
- token 过期自动 refresh，refresh 失败才算认证失效
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import secrets
import string
import time
from typing import Any

import httpx

BASE_URL = "https://ima.qq.com"
QA_PATH = "/cgi-bin/assistant/qa"
INIT_PATH = "/cgi-bin/session_logic/init_session"
REFRESH_PATH = "/cgi-bin/auth_login/refresh"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0"
)


class ImaError(Exception):
    """ima 调用失败（通用）。"""


class ImaAuthError(ImaError):
    """登录态失效，需要重新抓 cookie。"""


# 实测：cookie 里的 IMA-TOKEN 过期时，各接口回 `code=600001 登录过期，请重新登录`
_LOGIN_EXPIRED_CODES = {600001, 600002}


def _is_login_expired(data: dict) -> bool:
    """响应体是不是"登录态过期"（不同接口 code 与文案不完全一致，两条都认）。"""
    if not isinstance(data, dict):
        return False
    if data.get("code") in _LOGIN_EXPIRED_CODES:
        return True
    msg = str(data.get("msg") or data.get("message") or "")
    return "登录" in msg and any(k in msg for k in ("过期", "失效", "重新登录"))


def cookie_value(cookie: str, key: str) -> str:
    """从 Cookie 串里取一个字段的值。"""
    if not cookie:
        return ""
    m = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]*)", cookie)
    return m.group(1).strip() if m else ""


class ImaClient:
    """一次构造可复用；内部维护 session 缓存与 token 状态。"""

    def __init__(
        self,
        cookie: str,
        bkn: str,
        *,
        robot_type: str = "5",
        scene_type: str = "1",
        model_type: str = "4",
        timeout: float = 60.0,
        session_ttl: int = 1800,
    ):
        if not cookie or not bkn:
            raise ImaAuthError("缺少 x-ima-cookie 或 x-ima-bkn")
        # 配置面板/复制的 cookie 常带前后空格，而 HTTP 头值不允许前导空白
        # （实测 httpx 会直接抛 LocalProtocolError: Illegal header value）
        cookie = str(cookie).strip()
        bkn = str(bkn).strip()
        if not cookie or not bkn:
            raise ImaAuthError("缺少 x-ima-cookie 或 x-ima-bkn")
        self._cookie_tail = cookie  # 原样保留，刷新时重建
        self.bkn = bkn
        self.robot_type = robot_type
        self.scene_type = scene_type
        self.model_type = model_type
        self.timeout = timeout
        self.session_ttl = session_ttl

        self._uid = cookie_value(cookie, "IMA-UID")
        self._guid = cookie_value(cookie, "IMA-GUID") or "default_guid"
        self._token = cookie_value(cookie, "IMA-TOKEN")
        self._refresh_token = cookie_value(cookie, "IMA-REFRESH-TOKEN")
        self._token_deadline = 0.0
        self._refresh_tried = False  # 首次刷新只做一次，别每个请求都去打 refresh
        self._lock = asyncio.Lock()
        # kb_id -> (session_id, 生成时间)
        self._sessions: dict[str, tuple[str, float]] = {}
        self._last_toast_holder: list[str] = []
        self.last_toast: str = ""

    # ------------------------------------------------------------------ 认证

    def _cookie_header(self, with_token: bool = True) -> str:
        """重建 x-ima-cookie；token 用最新的（刷新后会变）。"""
        parts = [p.strip() for p in self._cookie_tail.split(";") if p.strip()]
        out = []
        for p in parts:
            if p.startswith("IMA-TOKEN="):
                if not with_token:
                    continue
                p = f"IMA-TOKEN={self._token}"
            out.append(p)
        if with_token and not any(x.startswith("IMA-TOKEN=") for x in out):
            out.append(f"IMA-TOKEN={self._token}")
        return "; ".join(out)

    def _headers(
        self, *, for_init: bool = False, for_refresh: bool = False
    ) -> dict[str, str]:
        head_accepted = for_init or for_refresh
        h = {
            "accept": "application/json" if head_accepted else "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json"
            if head_accepted
            else "text/event-stream",
            "extension_version": "999.999.999",
            "from_browser_ima": "1",
            "origin": "https://ima.qq.com",
            "referer": "https://ima.qq.com/wikis",
            "user-agent": _UA,
            "x-ima-bkn": self.bkn,
            "x-ima-cookie": self._cookie_header(with_token=not for_refresh),
        }
        if not for_refresh and self._token:
            h["authorization"] = f"Bearer {self._token}"
        return h

    async def refresh_token(self) -> bool:
        """用刷新令牌换新的访问令牌。失败返回 False（不是抛异常）。"""
        if not self._uid or not self._refresh_token:
            return False
        async with self._lock:
            try:
                async with httpx.AsyncClient(timeout=30.0) as c:
                    r = await c.post(
                        BASE_URL + REFRESH_PATH,
                        json={
                            "user_id": self._uid,
                            "refresh_token": self._refresh_token,
                        },
                        headers=self._headers(for_refresh=True),
                    )
                if r.status_code != 200:
                    return False
                data = r.json()
                if data.get("code") == 0 and data.get("token"):
                    self._token = data["token"]
                    ttl = int(data.get("token_valid_time") or 7200)
                    # 留 5 分钟余量
                    self._token_deadline = time.time() + max(ttl - 300, 60)
                    return True
                return False
            except Exception:  # noqa: BLE001
                return False

    async def ensure_token(self, force: bool = False) -> None:
        """确保手上有可用 token。

        🔴 实测（2026-09-20）：cookie 里的 `IMA-TOKEN` 往往**早就过期**了 ——
        直接用它会得到 `init_session 失败 code=600001 登录过期`；而同一份 cookie 里的
        `IMA-REFRESH-TOKEN` 通常还能换到新 token。所以**首次调用就要主动刷一次**，
        不能抱着"等真报错再刷"的想法 —— 那样 token 一过期整个插件就彻底罢工，
        直到有人重新手工抓 cookie。
        """
        now = time.time()
        if not force and self._token_deadline and now < self._token_deadline:
            return
        if not force and self._refresh_tried:
            return
        self._refresh_tried = True
        if await self.refresh_token():
            return
        if force:
            raise ImaAuthError("登录态已失效，请重新抓取 x-ima-cookie / x-ima-bkn")
        # 换不到新 token：先退回 cookie 自带的那份试一次，真不行上层会带 force 再来
        self._token_deadline = now + 600

    # ------------------------------------------------------------------ 会话

    async def _init_once(self, kb_id: str) -> dict[str, Any]:
        """打一次 init_session，返回响应体（HTTP / 非 JSON 直接抛）。"""
        # 注意：robotType / sceneType 这类枚举字段必须传**数字**，
        # 传字符串会得到 `unknown value "5" for enum ...RobotType`（已实测）
        body = {
            "envInfo": {"robotType": int(self.robot_type), "interactType": 0},
            "relatedUrl": kb_id,
            "sceneType": int(self.scene_type),
            "msgsLimit": 10,
            "forbidAutoAddToHistoryList": False,
            "knowledgeBaseInfoWithFolder": {
                "knowledge_base_id": kb_id,
                "folder_ids": [],
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(
                BASE_URL + INIT_PATH, json=body, headers=self._headers(for_init=True)
            )
        if r.status_code != 200:
            raise ImaError(f"init_session HTTP {r.status_code}: {r.text[:300]}")
        try:
            data = json.loads(r.text)
        except ValueError as e:
            ctype = r.headers.get("content-type", "?")
            raise ImaError(
                f"init_session 返回非 JSON（content-type={ctype}）：{r.text[:300]}"
            ) from e
        return data if isinstance(data, dict) else {}

    async def _get_session_id(self, kb_id: str) -> str:
        cached = self._sessions.get(kb_id)
        if cached and time.time() - cached[1] < self.session_ttl:
            return cached[0]
        await self.ensure_token()
        data = await self._init_once(kb_id)
        if _is_login_expired(data):
            # token 半路过期：强刷一次再原样重试（实测能救回来）
            await self.ensure_token(force=True)
            data = await self._init_once(kb_id)
        if _is_login_expired(data):
            raise ImaAuthError(
                f"登录态失效 code={data.get('code')} msg={data.get('msg')}"
            )
        if data.get("code") != 0 or not data.get("session_id"):
            raise ImaError(
                f"init_session 失败 code={data.get('code')} msg={data.get('msg')}"
            )
        sid = data["session_id"]
        self._sessions[kb_id] = (sid, time.time())
        return sid

    # ------------------------------------------------------------------ 问答

    @staticmethod
    def _temp_uskey() -> str:
        return base64.b64encode(secrets.token_bytes(32)).decode()

    def _qa_body(self, question: str, session_id: str) -> dict[str, Any]:
        guid = self._guid
        return {
            "session_id": session_id,
            "robot_type": int(self.robot_type),
            "question": question,
            "question_type": 2,
            "client_id": "".join(
                random.choices(string.ascii_lowercase + string.digits, k=24)
            ),
            "command_info": {
                "type": 14,
                "knowledge_qa_info": {"tags": [], "knowledge_ids": []},
            },
            "model_info": {
                "model_type": int(self.model_type),
                "enable_enhancement": False,
            },
            "history_info": {},
            "device_info": {
                "uskey": self._temp_uskey(),
                "uskey_bus_infos_input": f"{guid}_{int(time.time())}",
            },
        }

    def _extract_text(self, line: str) -> str:
        """从一行 SSE 里抠出正文片段；非正文行返回空串。"""
        s = line.strip()
        if not s:
            return ""
        if s.startswith("data:"):
            s = s[5:].strip()
        if not s or s == "[DONE]" or s.startswith(("event:", "id:", ":")):
            return ""
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return ""
        if not isinstance(obj, dict):
            return ""
        # 错误/提示帧（如"模型失效，已切换为默认模型"）：记下来，但不当正文
        for key in ("task_infos", "taskInfos"):
            for t in obj.get(key) or []:
                if isinstance(t, dict):
                    toast = (t.get("toast_task") or t.get("toastTask") or {}).get(
                        "content"
                    )
                    if toast:
                        self._last_toast_holder.append(str(toast))
        data = obj.get("Data") or obj.get("data")
        if isinstance(data, dict):
            tm = data.get("text_message") or data.get("textMessage")
            if isinstance(tm, dict) and isinstance(tm.get("Text"), str):
                return tm["Text"]
            # 兼容 blockMessage 的另一种写法
            if isinstance(data.get("Text"), str):
                return data["Text"]
        if isinstance(obj.get("Text"), str):
            return obj["Text"]
        if isinstance(obj.get("content"), str):
            return obj["content"]
        msgs = obj.get("msgs")
        if isinstance(msgs, list):
            return "".join(
                str(m.get("content", "")) for m in msgs if isinstance(m, dict)
            )
        return ""

    async def ask(
        self, question: str, kb_id: str, *, max_seconds: float | None = None
    ) -> str:
        """向指定知识库提问，返回拼好的纯文本答案。

        max_seconds: 硬超时（含建会话）。超时抛 ImaError，调用方自己降级。
        """
        question = (question or "").strip()
        if not question:
            raise ImaError("问题不能为空")

        async def _run() -> str:
            session_id = await self._get_session_id(kb_id)
            body = self._qa_body(question, session_id)
            parts: list[str] = []
            self._last_toast_holder = []
            async with (
                httpx.AsyncClient(timeout=self.timeout) as c,
                c.stream(
                    "POST", BASE_URL + QA_PATH, json=body, headers=self._headers()
                ) as r,
            ):
                if r.status_code != 200:
                    text = (await r.aread()).decode("utf-8", "replace")
                    raise ImaError(f"qa HTTP {r.status_code}: {text[:200]}")
                ctype = r.headers.get("content-type", "")
                if "text/event-stream" not in ctype:
                    text = (await r.aread()).decode("utf-8", "replace")
                    raise ImaError(f"qa 返回非流式: {ctype} {text[:200]}")
                async for line in r.aiter_lines():
                    frag = self._extract_text(line)
                    if frag:
                        parts.append(frag)
            self.last_toast = " / ".join(self._last_toast_holder)
            return "".join(parts).strip()

        if max_seconds:
            try:
                return await asyncio.wait_for(_run(), timeout=max_seconds)
            except asyncio.TimeoutError as e:
                raise ImaError(f"查询超过 {max_seconds:.0f} 秒未返回") from e
        return await _run()
