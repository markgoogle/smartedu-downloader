"""登录 Cookie 会话管理：可选输入平台 Cookie，解锁需鉴权的列表/搜索端点。

安全要点（生产要求）：
  * Cookie 仅存于进程内存（SessionStore），绝不写入 catalog.json / 日志 / API 响应；
    API 只回显脱敏摘要（names + mask）。
  * 平台 CDN 域白名单之外不发送任何 Cookie（httpx cookies= 参数按请求附带，
    天然只作用于当前出站 URL）。
  * 下载主链路（公开详情 JSON → 公开桶 PDF / 图片合成）**不需要** Cookie；
    Cookie 只用于「列表发现」通道（如 x-search 聚合接口，匿名时 403/418）。
  * 支持环境变量 SMARTEDU_COOKIE 兜底注入；运行时可 set/clear。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

COOKIE_ENV = "SMARTEDU_COOKIE"
_MAX_COOKIE_LEN = 8192
_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def parse_cookie(raw: str) -> dict[str, str]:
    """把浏览器复制的 `a=1; b=2` 或 `Cookie: a=1; b=2` 解析为 dict。

    仅接受合法 token 名；超长/超量条目截断（防滥用）。返回 {} 表示无效输入。
    """
    if not raw:
        return {}
    raw = raw.strip()
    if len(raw) > _MAX_COOKIE_LEN:
        raw = raw[:_MAX_COOKIE_LEN]
    m = re.match(r"(?i)^cookie:\s*", raw)
    if m:
        raw = raw[m.end():]
    out: dict[str, str] = {}
    for seg in raw.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        name, value = seg.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not _NAME_RE.match(name):
            continue
        out[name] = value[:1024]
        if len(out) >= 64:
            break
    return out


@dataclass
class Session:
    """一次配置的凭据（内存态）。"""
    cookies: dict[str, str] = field(default_factory=dict)
    source: str = ""          # manual | env
    updated_at: float = 0.0

    def masked(self) -> dict:
        """脱敏摘要——绝不包含 value。"""
        from time import time
        return {
            "configured": bool(self.cookies),
            "source": self.source,
            "names": sorted(self.cookies.keys()),
            "count": len(self.cookies),
            "ageSeconds": max(0, int(time() - self.updated_at)) if self.updated_at else None,
        }


class SessionStore:
    """进程内单例：保存当前登录 Cookie。"""

    def __init__(self):
        self._session = Session()
        env = os.environ.get(COOKIE_ENV, "").strip()
        if env:
            self.set(env, source="env")

    def set(self, raw_cookie: str, source: str = "manual") -> dict:
        cookies = parse_cookie(raw_cookie or "")
        self._session = Session(cookies=cookies, source=source if cookies else "",
                                updated_at=__import__("time").time() if cookies else 0.0)
        return self._session.masked()

    def clear(self) -> dict:
        self._session = Session()
        return self._session.masked()

    @property
    def session(self) -> Session:
        return self._session

    def cookie_header(self) -> Optional[str]:
        """拼回 `k=v; k2=v2`（供手动设置 header 的场景）。无凭据返回 None。"""
        if not self._session.cookies:
            return None
        return "; ".join(f"{k}={v}" for k, v in self._session.cookies.items())

    def has(self) -> bool:
        return bool(self._session.cookies)
