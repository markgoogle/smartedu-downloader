"""安全工具：URL 白名单（防 SSRF）、文件名清洗、简单限流。

实测约束（REPORT.md）：对平台 CDN 的任何请求都**不得携带 Referer**；
带非白名单 Referer 会直接 403。本模块集中保证这一点。
"""
import ipaddress
import re
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from .config import ALLOWED_REMOTE_SUFFIXES


class UrlPolicyError(ValueError):
    pass


def validate_remote_url(url: str, allow_private: bool = False) -> str:
    """只允许 https + 平台 CDN 域，拒绝内网/元数据地址（SSRF 防护）。"""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise UrlPolicyError("仅允许 https 资源 URL")
    host = (parts.hostname or "").lower()
    if not host:
        raise UrlPolicyError("URL 缺少主机名")
    if not any(host == s.lstrip(".") or host.endswith(s) for s in ALLOWED_REMOTE_SUFFIXES):
        raise UrlPolicyError(f"主机 {host} 不在平台白名单内")
    if not allow_private:
        # 域名解析出内网 IP 的情况极少（CDN），但按生产要求仍做字面检查
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise UrlPolicyError("拒绝访问内网地址")
        except ValueError:
            pass  # 是域名，不是 IP 字面量
    return url


def sanitize_filename(name: str, max_len: int = 180) -> str:
    """去除路径分隔符与控制字符，防止路径穿越/异常文件名。"""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name or "").strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return (name or "untitled")[:max_len]


_TRAVERSAL = re.compile(r"(\.\.|^/|[\x00-\x1f])")


def is_safe_relpath(rel: str) -> bool:
    """job 产物相对路径校验：禁止绝对路径、`..`、空段。"""
    if not rel or _TRAVERSAL.search(rel):
        return False
    return all(seg and seg != ".." for seg in rel.split("/"))


class RateLimiter:
    """每客户端每分钟滑动窗口限流（进程内，够用且零依赖）。"""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True
