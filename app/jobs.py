"""下载任务管理：内存 JobStore + 后台 asyncio worker，A→B 自动回退。

生产要点：
  * 任务总数上限（超限 429/409），完成任务按保留期回收（含临时页目录）
  * 单任务失败不影响其它任务；进度通过轮询 API 暴露
  * 产物路径一律相对化存储，读取时再校验防穿越
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from .config import settings, UUID_RE
from .downloader import Client, UpstreamError, get_meta, download_native, download_images

log = logging.getLogger("smartedu.jobs")


@dataclass
class Job:
    id: str
    cid: str
    mode: str                      # auto | native | images
    status: str = "queued"         # queued|running|done|error|cancelled
    step: str = ""
    downloaded: int = 0
    total: int = 0
    bytes: int = 0
    result: Optional[dict] = None
    error: Optional[str] = None
    log_lines: list = field(default_factory=list)
    created: float = field(default_factory=time.time)
    finished: Optional[float] = None
    task: Optional[asyncio.Task] = None

    def public(self) -> dict:
        return {k: getattr(self, k) for k in
                ("id", "cid", "mode", "status", "step", "downloaded", "total",
                 "bytes", "result", "error", "created")} | {"log": self.log_lines[-8:]}


class JobStore:
    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._client: Client | None = None

    async def init_client(self):
        if self._client is None:
            self._client = Client()
            await self._client.__aenter__()

    async def close(self):
        async with self._lock:
            for j in self._jobs.values():
                if j.task and not j.task.done():
                    j.task.cancel()
        if self._client:
            await self._client.__aexit__()
            self._client = None

    def get(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    def list_jobs(self) -> list[dict]:
        return [j.public() for j in sorted(self._jobs.values(),
                                           key=lambda x: -x.created)]

    async def create(self, cid: str, mode: str = "auto",
                     pages: Optional[tuple[int, int]] = None) -> Job:
        if not UUID_RE.match(cid.lower()):
            raise ValueError("contentId 必须是标准 UUID")
        cid = cid.lower()
        if mode not in ("auto", "native", "images"):
            raise ValueError("mode 仅支持 auto/native/images")
        if pages and (pages[0] < 1 or pages[1] < pages[0]
                      or pages[1] - pages[0] + 1 > settings.image_pages_limit):
            raise ValueError("页码范围非法")
        async with self._lock:
            active = [j for j in self._jobs.values()
                      if j.status in ("queued", "running")]
            if len(active) >= settings.max_jobs:
                raise RuntimeError(f"并发任务已达上限 {settings.max_jobs}，请稍后再试")
            self._sweep_locked()
            job = Job(id=uuid.uuid4().hex[:12], cid=cid, mode=mode)
            self._jobs[job.id] = job
            job.task = asyncio.create_task(self._run(job, pages))
        return job

    def cancel(self, jid: str) -> bool:
        j = self._jobs.get(jid)
        if j and j.status in ("queued", "running") and j.task:
            j.task.cancel()
            return True
        return False

    def _sweep_locked(self):
        now = time.time()
        stale = [jid for jid, j in self._jobs.items()
                 if j.finished and now - j.finished > settings.job_retain_seconds]
        for jid in stale:
            j = self._jobs.pop(jid)
            d = os.path.join(settings.download_dir, j.cid)
            shutil.rmtree(d, ignore_errors=True)

    async def sweep(self):
        async with self._lock:
            self._sweep_locked()

    async def _run(self, job: Job, pages):
        dest = os.path.join(settings.download_dir, job.cid)
        try:
            os.makedirs(dest, exist_ok=True)
            job.status = "running"
            job.step = "获取详情 JSON"
            meta = await get_meta(job.cid, self._client)
            job.log_lines.append(
                f"title={meta.title} pagesize={meta.pagesize} "
                f"predictHit={meta.native.predict_hit}")
            modes = ["native", "images"] if job.mode == "auto" else [job.mode]
            last_err = None
            for m in modes:
                try:
                    if m == "native":
                        if not meta.native.public_url:
                            raise UpstreamError("无 source PDF 条目")
                        job.step = "路径A：原生 PDF"
                        r = await download_native(
                            meta, dest, self._client,
                            progress=lambda got, tot: self._prog_native(job, got, tot))
                        res = {"path": relpath(dest, r.path), "bytes": r.bytes,
                               "mode": "native", "pages": meta.pagesize,
                               "title": meta.title,
                               "declaredBytes": meta.native.size,
                               "verified": "size+magic+EOF"}
                    else:
                        job.step = "路径B：逐页图片合成"
                        job.total = meta.pagesize
                        prange = range(pages[0], pages[1] + 1) if pages else None
                        r = await download_images(
                            meta, dest, self._client, pages=prange,
                            progress=lambda n, t: self._prog_pages(job, n, t))
                        res = {"path": relpath(dest, r.path), "bytes": r.bytes,
                               "mode": "images", "pages": r.pages,
                               "title": meta.title,
                               "boundaryStatus": r.boundary_status,
                               "jpegTotal": r.jpeg_total,
                               "imageDeclaredBytes": meta.image_total_size,
                               "verified": "pagesize+boundary"}
                    res["fallbackUsed"] = (m == "images" and "native" in modes)
                    job.result = res
                    job.status = "done"
                    job.step = "完成"
                    job.finished = time.time()
                    job.log_lines.append(f"path {m} ok → {res['path']}")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    last_err = str(e)
                    job.log_lines.append(f"path {m} failed: {last_err}")
                    if m == "native":
                        job.step = "路径A失败 → 回退路径B"
            job.status = "error"
            job.error = last_err or "所有路径均失败"
            job.finished = time.time()
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "已取消"
            job.finished = time.time()
        except Exception as e:  # noqa: BLE001
            log.exception("job %s crashed", job.id)
            job.status = "error"
            job.error = re.sub(r"/[^\s]+", "<path>", str(e))  # 不回显服务器路径
            job.finished = time.time()

    @staticmethod
    def _prog_native(job: Job, got: int, tot: int):
        job.bytes = got
        job.total = tot
        job.downloaded = int(got * 100 / tot) if tot else 0
        job.step = f"路径A：原生 PDF {got}/{tot} B"

    @staticmethod
    def _prog_pages(job: Job, n: int, t: int):
        job.downloaded = n
        job.total = t


def relpath(base: str, full: str) -> str:
    """结果只保存相对路径，避免泄漏绝对路径 & 便于服务端重映射。"""
    return os.path.relpath(full, base)
