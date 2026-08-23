"""LLM 客户端：chat（json_object 结构化输出）+ embedding。

- structured_output_mode=json_object：端点不支持 json_schema 时把 schema 注入 prompt
- 剥离 ```json 代码围栏、失败重试（退避）
- embedding 走同一端点的 /embeddings（默认 1024 维）
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 端点/模型均可用环境变量覆盖（.env），便于部署到其他机器：
#   YINOR_LLM_BASE_URL  OpenAI 兼容端点（默认本机网关）
#   YINOR_LLM_MODEL     提取/对话模型
#   YINOR_EMBED_MODEL   embedding 模型（1024 维）
DEFAULT_LLM_URL = os.environ.get("YINOR_LLM_BASE_URL", "http://127.0.0.1:20100/v1")
DEFAULT_MODEL = os.environ.get("YINOR_LLM_MODEL", "auto")
EMBED_MODEL = os.environ.get("YINOR_EMBED_MODEL", "text-embedding-v3")
try:
    EMBED_DIM = int(os.environ.get("YINOR_EMBED_DIM", "1024"))
except ValueError:
    EMBED_DIM = 1024

_STRIP_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\r?\n?|\r?\n?```[ \t]*$")


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(
        self,
        base_url: str = DEFAULT_LLM_URL,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        embed_model: str = EMBED_MODEL,
        embed_dim: int = EMBED_DIM,
        max_tokens: int = 8192,
        temperature: float = 0.1,
        max_retries: int = 4,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get(
            "CYROUTER_API_KEY"  # 兼容旧变量名
        )
        if not self.api_key:
            raise LLMError("缺少 LLM_API_KEY 环境变量（或传入 api_key）")
        self.model = model
        self.embed_model = embed_model
        self.embed_dim = embed_dim
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=httpx.Timeout(timeout)
        )

    # ---------- chat ----------

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        s = text.strip()
        if s.startswith("```"):
            s = _STRIP_FENCE_RE.sub("", s, count=1)
        return s.strip()

    async def _chat_once(
        self,
        messages: list[dict[str, str]],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """一次 chat 调用。response_model 非空时开启 json_object 模式并解析。"""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": self.temperature,
        }
        if response_model is not None:
            # 不注入完整 schema：prompt 已描述输出结构，注入大 schema 反而干扰模型（实测返回空）
            payload["response_format"] = {"type": "json_object"}

        resp = await self._client.post(
            "/chat/completions", json=payload, headers=self._headers()
        )
        resp.raise_for_status()
        data = resp.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(
                f"LLM 响应结构异常: {json.dumps(data, ensure_ascii=False)[:500]}"
            ) from e
        if not content:
            raise LLMError("LLM 返回空响应")
        if response_model is not None:
            text = self._strip_code_fences(content)
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                # 转为 LLMError，上层 chat() 重试循环会捕获后遇避重试
                raise LLMError(f"LLM 返回的 JSON 无法解析: {text[:300]}") from e
        return data

    async def chat(
        self,
        messages: list[dict[str, str]],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """带重试的 chat 调用（JSONDecodeError / 网络错误退避重试）。"""
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                return await self._chat_once(messages, response_model, max_tokens)
            except (json.JSONDecodeError, LLMError, httpx.HTTPError) as e:
                last_err = e
                wait = 1.5 * (2**attempt)
                logger.warning(
                    "LLM 调用失败 (attempt %d/%d): %s, %ss 后重试",
                    attempt + 1,
                    self.max_retries,
                    e,
                    wait,
                )
                if attempt < self.max_retries - 1:
                    await asyncio_sleep(wait)
        raise LLMError(f"LLM 调用多次失败: {last_err}") from last_err

    # ---------- embedding ----------

    async def embed(self, text: str) -> list[float]:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(
                    "/embeddings",
                    json={"model": self.embed_model, "input": text},
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                return data["data"][0]["embedding"][: self.embed_dim]
            except httpx.HTTPError as e:
                last_err = e
                logger.warning("embed 失败 (attempt %d/3): %s", attempt + 1, e)
                if attempt < 2:
                    await asyncio_sleep(1.5 * (2**attempt))
        raise LLMError(f"embed 多次失败: {last_err}") from last_err

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 上游批量限制：>15 条可能 400，分批 10 条；每块独立重试
        out: list[list[float]] = []
        for i in range(0, len(texts), 10):
            chunk = texts[i : i + 10]
            last_err: Exception | None = None
            for attempt in range(3):
                try:
                    resp = await self._client.post(
                        "/embeddings",
                        json={"model": self.embed_model, "input": chunk},
                        headers=self._headers(),
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    out.extend(e["embedding"][: self.embed_dim] for e in data["data"])
                    break
                except httpx.HTTPError as e:
                    last_err = e
                    logger.warning(
                        "embed_batch 块失败 (attempt %d/3): %s", attempt + 1, e
                    )
                    if attempt < 2:
                        await asyncio_sleep(1.5 * (2**attempt))
            else:
                raise LLMError(f"embed_batch 块多次失败: {last_err}") from last_err
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
