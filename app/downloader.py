"""smartedu 教材下载核心库（异步，httpx）。

流程与 REPORT.md 实测结论一一对应：
  ① 公开详情 JSON（免登录/Token/Referer）→ 元数据
  ② 路径 A：ti_storages 私有域直链去掉 "-private" → 公开桶原生 PDF（约 1/3 命中）
  ③ 路径 B：preview 模板替换页码逐页 JPG → /DCTDecode 原样合成 PDF（100% 可用）
  校验：Content-Length == ti_size、%PDF- 魔数、%%EOF 尾部、页数 == pagesize、
        pagesize+1 页边界应 403。
安全：所有出站 URL 过白名单；绝不发送 Referer。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlsplit, urlparse

import httpx

from .config import DETAIL_PATH_TMPL, S_FILE_HOSTS, UA, settings
from .security import UrlPolicyError, validate_remote_url

log = logging.getLogger("smartedu")

# 样本内判据阈值：源 PDF 文件名尾部毫秒时间戳 >= 2026-08-01T00:00+08:00 → 公开桶命中
TS_HIT_THRESHOLD = 1785513600000


class UpstreamError(RuntimeError):
    """上游端点返回语义错误（403/404/网络故障）。"""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class NativeInfo:
    available_meta: bool = False
    size: Optional[int] = None
    md5: Optional[str] = None
    filename: Optional[str] = None
    public_url: Optional[str] = None
    upload_ts: Optional[int] = None
    predict_hit: Optional[bool] = None


@dataclass
class Meta:
    content_id: str
    title: str
    pagesize: int = 0
    subject: Optional[str] = None
    grade: Optional[str] = None
    volume: Optional[str] = None
    stage: Optional[str] = None
    publisher: Optional[str] = None
    update_time: Optional[str] = None
    native: NativeInfo = field(default_factory=NativeInfo)
    image_template: Optional[str] = None
    image_total_size: Optional[int] = None
    preview_count: int = 0

    def to_dict(self) -> dict:
        return {
            "contentId": self.content_id,
            "title": self.title,
            "pagesize": self.pagesize,
            "subject": self.subject, "grade": self.grade, "volume": self.volume,
            "stage": self.stage, "publisher": self.publisher,
            "updateTime": self.update_time,
            "native": {
                "availableMeta": self.native.available_meta,
                "size": self.native.size, "md5": self.native.md5,
                "filename": self.native.filename,
                "predictHit": self.native.predict_hit,
            },
            "images": {
                "pagesize": self.pagesize,
                "totalSize": self.image_total_size,
                "previewCount": self.preview_count,
                "templateAvailable": bool(self.image_template),
            },
        }


def _req_val(item: dict, name: str):
    for r in (item.get("custom_properties") or {}).get("requirements") or []:
        if r.get("name") == name:
            return r.get("value")
    return None


def parse_detail(detail: dict) -> Meta:
    """把详情 JSON 解析为内部 Meta（纯函数，可单测）。"""
    cid = detail["id"]
    items = detail.get("ti_items") or []
    src = next((it for it in items
                if it.get("ti_file_flag") == "source" and it.get("ti_format") == "pdf"), None)
    img = next((it for it in items if it.get("ti_file_flag") == "image"), None)

    pagesize = int(_req_val(img or src or {}, "pagesize") or 0)
    preview = (detail.get("custom_properties") or {}).get("preview") or {}
    image_template = None
    if preview:
        try:
            kmin = min(preview, key=lambda k: int(re.sub(r"\D", "", k) or 0))
            first = preview[kmin]
        except Exception:
            first = next(iter(preview.values()))
        image_template = re.sub(r"/[^/]*\.jpg(\?.*)?$", "/{page}.jpg", first)

    native = NativeInfo()
    if src and src.get("ti_storages"):
        priv = src["ti_storages"][0]
        native.available_meta = True
        native.size = src.get("ti_size")
        native.md5 = src.get("ti_md5")
        native.public_url = priv.replace("-ndr-private.", "-ndr.")
        fname = os.path.basename(urlparse(native.public_url).path)
        native.filename = fname
        m = re.search(r"_(\d{13})\.pdf$", fname)
        if m:
            native.upload_ts = int(m.group(1))
            native.predict_hit = native.upload_ts >= TS_HIT_THRESHOLD

    tags = {t.get("tag_dimension_id"): t.get("tag_name")
            for t in detail.get("tag_list") or []}
    meta = Meta(
        content_id=cid,
        title=(detail.get("global_title") or {}).get("zh-CN") or cid,
        pagesize=pagesize,
        subject=tags.get("zxxxk"), grade=tags.get("zxxnj"), volume=tags.get("zxxcc"),
        stage=tags.get("zxxxd"), publisher=tags.get("zxxbb"),
        update_time=detail.get("update_time"),
        native=native,
        image_template=image_template,
        image_total_size=(img or {}).get("ti_size"),
        preview_count=len(preview),
    )
    # 防御性校验：URL 必须来自白名单域
    for u in filter(None, [native.public_url, image_template and image_template.replace("{page}", "1")]):
        try:
            validate_remote_url(u, allow_private=settings.allow_private_hosts)
        except UrlPolicyError as e:
            log.warning("reject non-whitelist url in %s: %s", cid, e)
            if u == native.public_url:
                native.public_url = None
            else:
                meta.image_template = None
    return meta


def encode_url(url: str) -> str:
    """对 path 做一次 percent-encode（中文/空格文件名；实测 `+` 当空格会 403）。"""
    parts = urlsplit(url)
    return parts._replace(path=quote(parts.path)).geturl()


def rotate_host(url: str, idx: int) -> str:
    if re.match(r"https://s-file-\d+", url):
        return re.sub(r"https://s-file-\d+", S_FILE_HOSTS[idx % 3], url, count=1)
    m = re.match(r"https://r(\d)-ndr(-private)?\.", url)
    if m:
        kind = "-ndr-private." if m.group(2) else "-ndr."
        return re.sub(r"https://r\d-ndr(-private)?\.", f"https://r{idx % 3 + 1}{kind}", url, count=1)
    return url


_HEADERS = {"User-Agent": UA, "Accept": "*/*"}  # 绝不含 Referer！


class Client:
    """封装 httpx.AsyncClient：主机轮换重试 + 语义化错误。"""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._own = client is None

    async def __aenter__(self):
        if self._own:
            self._client = httpx.AsyncClient(
                headers=_HEADERS, timeout=httpx.Timeout(settings.http_timeout),
                follow_redirects=False, limits=httpx.Limits(max_connections=32))
        return self

    async def __aexit__(self, *exc):
        if self._own and self._client:
            await self._client.aclose()

    async def get(self, url: str, *, timeout: float | None = None,
                  stream: bool = False, max_retry: int = 2) -> httpx.Response:
        last_exc = None
        for attempt in range(max_retry + 1):
            u = encode_url(rotate_host(url, attempt))
            try:
                if stream:
                    req = self._client.build_request("GET", u, timeout=timeout)
                    return await self._client.send(req, stream=True)
                return await self._client.get(u, timeout=timeout)  # 403/401/404 语义结果交调用方判断
            except (httpx.TransportError,) as e:
                last_exc = e
                await asyncio.sleep(0.5 * (attempt + 1))
        raise UpstreamError(f"网络故障: {last_exc}")

    async def head_length(self, url: str) -> Optional[int]:
        """HEAD 只用于取 Content-Length（私有域 HEAD 会 200 但不构成下载通路，仅记录）。"""
        try:
            resp = await self.get(url, timeout=min(15.0, settings.http_timeout))
            cl = resp.headers.get("content-length")
            return int(cl) if cl and cl.isdigit() else None
        except Exception:
            return None


async def fetch_detail_json(cid: str, client: Client) -> dict:
    base_path = DETAIL_PATH_TMPL.format(cid=cid)
    err_status = None
    for i, host in enumerate(S_FILE_HOSTS):
        url = f"https://{host}.ykt.cbern.com.cn{base_path}"
        validate_remote_url(url, allow_private=settings.allow_private_hosts)
        resp = await client.get(url, timeout=min(20.0, settings.http_timeout))
        if resp.status_code == 200:
            return resp.json()
        err_status = resp.status_code
        if err_status != 403 or i > 0:
            break
    raise UpstreamError(
        f"详情 JSON 获取失败 HTTP {err_status}（该资源可能不是教材或已下线）",
        status=err_status)


async def get_meta(cid: str, client: Client) -> Meta:
    return parse_detail(await fetch_detail_json(cid, client))


# ---------------------------------------------------------------------------
# 路径 A：原生 PDF
# ---------------------------------------------------------------------------

@dataclass
class NativeResult:
    path: str
    bytes: int
    pages: Optional[int] = None
    mode: str = "native"


async def download_native(meta: Meta, dest_dir: str, client: Client,
                          progress=None) -> NativeResult:
    url = meta.native.public_url
    if not url:
        raise UpstreamError("无 source PDF 条目")
    expect = meta.native.size
    fname = meta.native.filename or f"{meta.content_id}.pdf"
    out = os.path.join(dest_dir, re.sub(r"\.pdf$", "", fname)[:150] + ".pdf")
    err_status = None
    for attempt in range(3):
        u = rotate_host(url, attempt)
        validate_remote_url(u, allow_private=settings.allow_private_hosts)
        resp = await client.get(u, stream=True, timeout=settings.http_timeout * 4)
        try:
            if resp.status_code not in (200, 206):
                err_status = resp.status_code
                continue
            cl = resp.headers.get("content-length")
            total = int(cl) if cl and cl.isdigit() else None
            got = 0
            head = b""
            tmp = out + ".part"
            with open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 16):
                    if got == 0:
                        head = chunk[:5]
                    got += len(chunk)
                    f.write(chunk)
                    if progress and total:
                        progress(got, total)
            # —— 完整性校验 ——
            if head != b"%PDF-":
                os.remove(tmp)
                raise UpstreamError("响应不是 PDF（魔数缺失）")
            if expect and got != int(expect):
                os.remove(tmp)
                raise UpstreamError(f"字节数不符：期望 {expect}，实际 {got}")
            with open(tmp, "rb") as f:
                f.seek(max(0, got - 1024))
                tail = f.read()
            if b"%%EOF" not in tail:
                os.remove(tmp)
                raise UpstreamError("PDF 尾部缺少 %%EOF")
            os.replace(tmp, out)
            return NativeResult(path=out, bytes=got)
        finally:
            await resp.aclose()
    raise UpstreamError(
        f"路径A失败 HTTP {err_status}（公开桶 AccessDenied → 回退图片合成）",
        status=err_status)


# ---------------------------------------------------------------------------
# 路径 B：逐页 JPG → 合成 PDF（标准库写出器，原始 JPEG 字节零重编码）
# ---------------------------------------------------------------------------

def jpeg_size(data: bytes):
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9):
            i += 2
            continue
        ln = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + ln
    return 1488, 2104  # 兜底：实测转码图统一尺寸


def build_pdf(jpg_paths: list[str], out_path: str, title: str = "") -> dict:
    """把 JPEG 以 /DCTDecode 原样嵌入 PDF。objects: catalog, pages, per page 3 objs."""
    objects: list[tuple[int, bytes]] = []
    num = 1
    catalog_n, pages_n = num, num + 1
    num += 2
    page_nums = []
    dims = []
    for jp in jpg_paths:
        with open(jp, "rb") as f:
            data = f.read()
        w, h = jpeg_size(data)
        dims.append((w, h))
        xobj_n = num
        num += 1
        objects.append((xobj_n,
                        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                        b"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
                        b"/Filter /DCTDecode /Length %d >>\nstream\n" % (w, h, len(data))
                        + data + b"\nendstream"))
        content = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (w, h)
        content_n = num
        num += 1
        objects.append((content_n,
                        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"))
        page_n = num
        num += 1
        objects.append((page_n,
                        b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
                        b"/Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R >>"
                        % (pages_n, w, h, xobj_n, content_n)))
        page_nums.append(page_n)

    kids = " ".join(f"{p} 0 R" for p in page_nums).encode()
    objects.append((pages_n, b"<< /Type /Pages /Kids [ %s ] /Count %d >>"
                    % (kids, len(page_nums))))
    objects.append((catalog_n, b"<< /Type /Catalog /Pages %d 0 R >>" % pages_n))
    info_n = num
    safe_title = re.sub(rb"[^\x20-\x7e]", b"", title.encode("utf-8", "ignore")[:200])
    objects.append((info_n,
                    b"<< /Title (%s) /Producer (smartedu-dl) /CreationDate (D:%s) >>"
                    % (safe_title, time.strftime("%Y%m%d%H%M%S").encode())))

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for n, body in sorted(objects, key=lambda x: x[0]):
        offsets[n] = len(buf)
        buf += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref_off = len(buf)
    maxn = max(offsets)
    buf += b"xref\n0 %d\n" % (maxn + 1)
    buf += b"0000000000 65535 f \n"
    for n in range(1, maxn + 1):
        buf += b"%010d 00000 n \n" % offsets.get(n, 0)
    buf += (b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (maxn + 1, catalog_n, info_n, xref_off))
    with open(out_path, "wb") as f:
        f.write(bytes(buf))
    return {"bytes": len(buf), "pages": len(jpg_paths), "dims": dims}


@dataclass
class ImagesResult:
    path: str
    bytes: int
    pages: int
    mode: str = "images"
    boundary_status: Optional[int] = None
    jpeg_total: int = 0


async def download_images(meta: Meta, dest_dir: str, client: Client,
                          pages: Optional[range] = None, progress=None) -> ImagesResult:
    tpl = meta.image_template
    if not tpl:
        raise UpstreamError("详情 JSON 中没有 preview 图片模板，无法走图片路径")
    total_pages = meta.pagesize
    if total_pages <= 0 or total_pages > settings.image_pages_limit:
        raise UpstreamError(f"页数异常（{total_pages}），超出允许上限 {settings.image_pages_limit}")
    plist = list(pages) if pages else list(range(1, total_pages + 1))
    if any(p < 1 or p > total_pages for p in plist):
        raise UpstreamError("页码范围越界")
    tmpdir = os.path.join(dest_dir, "_pages_" + re.sub(r"\W", "", tpl[-20:])[:12]
                          + time.strftime("%H%M%S"))
    os.makedirs(tmpdir, exist_ok=True)
    sem = asyncio.Semaphore(settings.page_concurrency)
    done = {"n": 0, "bytes": 0}

    async def fetch_page(p: int) -> str:
        url = tpl.replace("{page}", str(p))
        validate_remote_url(url, allow_private=settings.allow_private_hosts)
        async with sem:
            last = None
            for attempt in range(3):
                resp = await client.get(rotate_host(url, attempt + p),
                                        timeout=settings.http_timeout * 2)
                if resp.status_code == 200 and resp.content[:2] == b"\xff\xd8":
                    path = os.path.join(tmpdir, f"{p:05d}.jpg")
                    with open(path, "wb") as f:
                        f.write(resp.content)
                    done["n"] += 1
                    done["bytes"] += len(resp.content)
                    if progress:
                        progress(done["n"], len(plist))
                    return path
                last = resp.status_code
            raise UpstreamError(f"第 {p} 页下载失败 HTTP {last}", status=last)

    paths = await asyncio.gather(*[fetch_page(p) for p in plist])

    out = os.path.join(dest_dir, "book.pdf")
    res = build_pdf(paths, out, title=meta.title)
    # 边界校验：pagesize+1 页应 403（实测特征；仅全量下载时做）
    boundary = None
    if not pages:
        resp = await client.get(tpl.replace("{page}", str(total_pages + 1)),
                                timeout=min(15.0, settings.http_timeout))
        boundary = resp.status_code
    return ImagesResult(path=out, bytes=res["bytes"], pages=res["pages"],
                        boundary_status=boundary, jpeg_total=done["bytes"])
