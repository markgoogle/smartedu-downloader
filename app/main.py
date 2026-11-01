"""FastAPI 应用：REST API + 静态 SPA。

生产特性：安全响应头、CSP、限流、请求体上限、健康检查、结构化错误、
仅本机默认绑定、产物下载走相对路径校验（防穿越）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .catalog import Catalog, DIM_KEYS
from .config import UUID_RE, settings
from .downloader import UpstreamError, get_meta, Client
from .jobs import JobStore, relpath
from .security import RateLimiter, is_safe_relpath, sanitize_filename

log = logging.getLogger("smartedu.api")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; "
       "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
       "object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.download_dir, exist_ok=True)
    os.makedirs(settings.data_dir, exist_ok=True)
    app.state.catalog = Catalog(settings.data_dir)
    app.state.catalog.reload()
    app.state.store = JobStore()
    await app.state.store.init_client()
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    sweeper = asyncio.create_task(_sweep_loop(app.state.store))
    yield
    sweeper.cancel()
    await app.state.store.close()


async def _sweep_loop(store: JobStore):
    while True:
        try:
            await asyncio.sleep(300)
            await store.sweep()
        except asyncio.CancelledError:
            break
        except Exception:
            log.exception("sweep failed")


app = FastAPI(title="smartedu textbook downloader", version="2.0.0",
              lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # 限流（按客户端 IP）
    ip = request.client.host if request.client else "unknown"
    limiter: RateLimiter = request.app.state.limiter
    if not limiter.allow(ip):
        return JSONResponse({"error": "rate limited"}, status_code=429,
                            headers={"Retry-After": "60"})
    # 拒绝过大请求体
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > 64 * 1024:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def validate_cid(cid: str) -> str:
    cid = (cid or "").strip().lower()
    if not UUID_RE.match(cid):
        raise HTTPException(400, "contentId 必须是标准 UUID（形如 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx）")
    return cid


def http_client(request: Request) -> Client:
    """共享的出站 httpx 封装（由 JobStore 在 lifespan 中创建/关闭）。"""
    return request.app.state.store._client


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health(request: Request):
    cat: Catalog = request.app.state.catalog
    store: JobStore = request.app.state.store
    return {"ok": True, "time": time.time(),
            "catalog": cat.status(),
            "jobs": {"active": sum(1 for j in store.list_jobs()
                                   if j["status"] in ("queued", "running"))}}


@app.get("/api/catalog")
async def api_catalog(request: Request,
                      q: str = Query("", max_length=100),
                      offset: int = Query(0, ge=0, le=10 ** 7),
                      limit: int = Query(24, ge=1, le=200),
                      stage: str = Query("", max_length=50),
                      subject: str = Query("", max_length=50),
                      grade: str = Query("", max_length=50),
                      volume: str = Query("", max_length=50),
                      version: str = Query("", max_length=50)):
    cat: Catalog = request.app.state.catalog
    filters = {k: v for k, v in
               zip(DIM_KEYS.keys(), (stage, subject, grade, volume, version))}
    res = cat.search(q, filters, offset, limit)
    res["catalogSize"] = cat.size
    return res


@app.post("/api/catalog/reload")
async def api_catalog_reload(request: Request):
    cat: Catalog = request.app.state.catalog
    n = cat.reload()
    return {"entries": n}


@app.get("/api/detail")
async def api_detail(request: Request, contentId: str = Query(..., max_length=64),
                     client: Client = Depends(http_client)):
    cid = validate_cid(contentId)
    try:
        meta = await get_meta(cid, client)
    except UpstreamError as e:
        raise HTTPException(502, str(e))
    d = meta.to_dict()
    tags = request.app.state.catalog.get_tags(cid)
    if tags:
        d["catalogTags"] = tags
    return d


class DownloadReq(BaseModel):
    contentId: str
    mode: str = Field("auto", pattern="^(auto|native|images)$")
    pages: str | None = Field(None, max_length=20)  # "1-5"


@app.post("/api/download")
async def api_download(req: DownloadReq, request: Request):
    cid = validate_cid(req.contentId)
    pages = None
    if req.pages:
        m = re.fullmatch(r"(\d+)-(\d+)", req.pages)
        if not m:
            raise HTTPException(400, "pages 需为 '起始-结束' 形式")
        pages = (int(m.group(1)), int(m.group(2)))
    store: JobStore = request.app.state.store
    try:
        job = await store.create(cid, req.mode, pages)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(429, str(e))
    return {"jobId": job.id}


@app.get("/api/jobs")
async def api_jobs(request: Request):
    return {"jobs": request.app.state.store.list_jobs()}


@app.get("/api/job")
async def api_job(request: Request, id: str = Query(..., min_length=6, max_length=32)):
    job = request.app.state.store.get(id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.public()


@app.post("/api/job/cancel")
async def api_job_cancel(request: Request, id: str = Query(..., min_length=6, max_length=32)):
    ok = request.app.state.store.cancel(id)
    return {"cancelled": ok}


@app.get("/api/file")
async def api_file(request: Request, job: str = Query(..., min_length=6, max_length=32),
                   inline: bool = Query(False)):
    store: JobStore = request.app.state.store
    j = store.get(job)
    if not j or not j.result:
        raise HTTPException(404, "job not ready")
    rel = j.result["path"]
    if not is_safe_relpath(rel):
        raise HTTPException(400, "bad path")
    full = os.path.realpath(os.path.join(settings.download_dir, j.cid, rel))
    base = os.path.realpath(os.path.join(settings.download_dir, j.cid))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        raise HTTPException(404, "file gone")
    title = sanitize_filename(j.result.get("title") or j.cid)
    fname = f"{title}.pdf"
    headers = {"Content-Disposition":
               f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(fname)}"}
    return FileResponse(full, media_type="application/pdf", headers=headers)


# ---------------------------------------------------------------------------
# 静态 SPA（web/dist，带 hash 资源长缓存；index.html 不缓存）
# ---------------------------------------------------------------------------

_HASHED = re.compile(r".*-[A-Za-z0-9_]{8,}\.(js|css)$")


@app.get("/{path:path}", include_in_schema=False)
async def spa(request: Request, path: str):
    static = settings.static_dir
    if path in ("", "index.html"):
        fp = os.path.join(static, "index.html")
        if os.path.isfile(fp):
            return FileResponse(fp, media_type="text/html;charset=utf-8",
                                headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, "frontend not built; run: cd web && npm run build")
    fp = os.path.realpath(os.path.join(static, path))
    if not fp.startswith(os.path.realpath(static) + os.sep) or not os.path.isfile(fp):
        # API 路径未被上面匹配时 → 明确 404 JSON
        if path.startswith("api/"):
            raise HTTPException(404, "not found")
        # SPA fallback
        idx = os.path.join(static, "index.html")
        if os.path.isfile(idx):
            return FileResponse(idx, media_type="text/html;charset=utf-8",
                                headers={"Cache-Control": "no-cache"})
        raise HTTPException(404, "not found")
    ext = os.path.splitext(fp)[1]
    ctype = {".html": "text/html;charset=utf-8", ".js": "text/javascript;charset=utf-8",
             ".css": "text/css;charset=utf-8", ".svg": "image/svg+xml",
             ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2"}.get(
        ext, "application/octet-stream")
    cache = "public, max-age=31536000, immutable" if _HASHED.match(path) else "no-cache"
    return FileResponse(fp, media_type=ctype, headers={"Cache-Control": cache})


@app.exception_handler(Exception)
async def unhandled_exc(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"error": "internal server error"}, status_code=500)
