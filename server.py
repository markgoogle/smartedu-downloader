#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smartedu 教材在线浏览 + 一键下载网站（本地服务器）

设计依据（2026-11-01 实测验证，见 REPORT.md / README.md）：
  * 公开详情 JSON：免登录、免 Token、免 Referer
      https://s-file-{1,2,3}.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{contentId}.json
  * 路径 A（原生 PDF）：把私有域 "-ndr-private." 换成公开域 "-ndr."；约 1/3 命中，其余 403 AccessDenied
  * 路径 B（图片合成）：preview URL 正则替换页码，pagesize 取 ti_items[image].requirements.pagesize
  * 关键坑：**绝不发送 Referer**（带非白名单 Referer 会 403）；文件名需 quote()；
    pagesize+1 页应 403（边界校验）；ti_size 是权威字节数。

依赖：仅标准库（http.server + urllib），无需 pip install。
用法：python3 server.py [--port 8000] [--data-dir ./catalog_data]
"""

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DATA_DIR_DEFAULT = os.path.join(HERE, "catalog_data")

S_FILE_HOSTS = ["s-file-1", "s-file-2", "s-file-3"]
NDR_HOSTS = ["r1-ndr", "r2-ndr", "r3-ndr"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------------------
# HTTP helpers —— 注意：任何情况下都不发送 Referer（实测带错 Referer → 403）
# ---------------------------------------------------------------------------

def http_get(url, timeout=20, headers=None, max_retry=2):
    """GET，返回 (status, body_bytes, header_dict)。网络错误时轮换 host 重试。"""
    last = None
    for attempt in range(max_retry + 1):
        hdrs = {"User-Agent": UA, "Accept": "*/*"}
        if headers:
            hdrs.update(headers)
        req = Request(url, headers=hdrs)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except HTTPError as e:
            body = e.read()
            # 403/401/404 是语义结果，不是网络故障，直接返回给调用方判断
            return e.code, body, dict(e.headers or {})
        except (URLError, OSError) as e:
            last = e
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"network failure: {last} ({url})")


def rotate_host(url, idx):
    """在等价分片域名之间轮换（实测 r1/r2/r3 与 s-file-1/2/3 内容逐字节一致）。"""
    if re.match(r"https://s-file-\d+", url):
        return re.sub(r"https://s-file-\d+", S_FILE_HOSTS[idx % 3], url, count=1)
    m = re.match(r"https://r\d-ndr(-private)?\.", url)
    if m:
        kind = "-ndr-private." if m.group(1) else "-ndr."
        return re.sub(r"https://r\d-ndr(-private)?\.",
                      f"https://r{idx % 3 + 1}{kind}", url, count=1)
    return url


def public_url(private_url):
    """私有域直链 → 公开桶直链（去掉 -private）。"""
    return private_url.replace("-ndr-private.", "-ndr.")


def fetch_details(cid):
    """拉取公开详情 JSON（教材口径实测 100% 可读；非教材约 28% 403）。"""
    cid = cid.strip().lower()
    if not re.fullmatch(r"[0-9a-f-]{16,64}", cid):
        raise ValueError("contentId 格式不正确")
    base_path = f"/zxx/ndrv2/resources/tch_material/details/{cid}.json"
    err = None
    for i, host in enumerate(S_FILE_HOSTS):
        url = f"https://{host}.ykt.cbern.com.cn{base_path}"
        st, body, _ = http_get(url, timeout=20)
        if st == 200:
            return json.loads(body.decode("utf-8"))
        err = (st, body[:200])
        if st == 403 and i == 0:
            continue  # 换分片再试一次
        break
    raise RuntimeError(f"详情 JSON 获取失败 HTTP {err[0]}（该资源可能不是教材或已下线）")


# ---------------------------------------------------------------------------
# 元数据解析（对齐实测结论）
# ---------------------------------------------------------------------------

def _req_val(item, name):
    for r in (item.get("custom_properties") or {}).get("requirements") or []:
        if r.get("name") == name:
            return r.get("value")
    return None


def parse_meta(detail):
    cid = detail["id"]
    items = detail.get("ti_items") or []
    src = next((it for it in items
                if it.get("ti_file_flag") == "source" and it.get("ti_format") == "pdf"), None)
    img = next((it for it in items if it.get("ti_file_flag") == "image"), None)

    pagesize = int(_req_val(img or src or {}, "pagesize") or 0) if (img or src) else 0
    preview = (detail.get("custom_properties") or {}).get("preview") or {}
    p_first = None
    if preview:
        # Slide 键无序，取编号最小的一条作为模板
        try:
            kmin = min(preview, key=lambda k: int(re.sub(r"\D", "", k) or 0))
            p_first = preview[kmin]
        except Exception:
            p_first = next(iter(preview.values()))

    image_template = None
    if p_first:
        image_template = re.sub(r"/[^/]*\.jpg$", "/{page}.jpg", p_first)

    native_public = public_url(src["ti_storages"][0]) if (src and src.get("ti_storages")) else None
    filename = os.path.basename(urlparse(native_public).path) if native_public else None

    ts = None
    if filename:
        m = re.search(r"_(\d{13})\.pdf$", filename)
        if m:
            ts = int(m.group(1))
    # 样本内判据：文件名尾部毫秒时间戳 >= 2026-08-01 → 公开桶命中（34 TP / 71 TN，仅提示用）
    predict_hit = (ts is not None and ts >= 1785513600000)  # 2026-08-01T00:00+08:00 ≈ 1785513600000

    tags = {}
    for t in detail.get("tag_list") or []:
        tags[t.get("tag_dimension_id")] = t.get("tag_name")

    title = (detail.get("global_title") or {}).get("zh-CN") or cid
    thumb = None
    cps = (detail.get("custom_properties") or {}).get("sys_resource_space")
    return {
        "contentId": cid,
        "title": title,
        "tags": tags,
        "publisher": tags.get("zxxbb"),
        "subject": tags.get("zxxxk"),
        "grade": tags.get("zxxnj"),
        "volume": tags.get("zxxcc"),
        "stage": tags.get("zxxxd"),
        "pagesize": pagesize,
        "native": {
            "available_meta": bool(src),
            "size": (src or {}).get("ti_size"),
            "md5": (src or {}).get("ti_md5"),
            "filename": filename,
            "public_url": native_public,
            "upload_ts": ts,
            "predict_hit": predict_hit if ts is not None else None,
        },
        "images": {
            "template": image_template,
            "pagesize": pagesize,
            "total_size": (img or {}).get("ti_size"),
            "preview_count": len(preview),
        },
        "thumbnail": (detail.get("custom_properties") or {}).get("thumbnails", [None])[0]
        if isinstance((detail.get("custom_properties") or {}).get("thumbnails"), list) else None,
        "update_time": detail.get("update_time"),
        "declared_bytes": cps,
    }


# ---------------------------------------------------------------------------
# 下载任务
# ---------------------------------------------------------------------------

JOBS = {}          # job_id -> dict
JOBS_LOCK = threading.Lock()
DOWNLOAD_DIR = os.path.join(HERE, "downloads")


def set_job(job_id, **kw):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def download_native(meta, dest_dir, job_id=None):
    """路径 A：公开桶原生 PDF。校验 Content-Length == ti_size、%PDF-、startxref+%%EOF。"""
    url = meta["native"]["public_url"]
    expect = meta["native"]["size"]
    fname = meta["native"]["filename"] or f"{meta['contentId']}.pdf"
    qurl = re.sub(r"(https://[^/]+/)(.*)", lambda m: m.group(1) + quote(m.group(2)), url)
    err_status = None
    for i in range(3):
        u = rotate_host(qurl, i)
        st, body, hdrs = http_get(u, timeout=180)
        if st in (200, 206) and body[:5] == b"%PDF-":
            if expect and len(body) != int(expect):
                raise RuntimeError(f"字节数不符：期望 {expect}，实际 {len(body)}")
            if b"%%EOF" not in body[-1024:]:
                raise RuntimeError("PDF 尾部缺少 %%EOF")
            out = os.path.join(dest_dir, sanitize(fname))
            with open(out, "wb") as f:
                f.write(body)
            return {"path": out, "bytes": len(body), "mode": "native",
                    "pages": None}
        err_status = st
    raise RuntimeError(f"路径A失败 HTTP {err_status}（公开桶无此对象，AccessDenied → 请回退图片合成）")


def build_pdf_from_images(jpg_paths, out_path, titles=("国家中小学智慧教育平台教材", "")):
    """纯标准库：把 JPEG 以 /DCTDecode 原样嵌入 PDF（不重编码，保留原始字节）。"""
    objects = []          # (num, bytes)
    num = 1
    catalog_n, pages_n = num, num + 1
    num += 2
    page_nums, xobj_nums, img_dims = [], [], []
    for jp in jpg_paths:
        data = open(jp, "rb").read()
        w, h = jpeg_size(data)
        img_dims.append((w, h))
        xobj_n = num; page_n = num + 1
        num += 2
        objects.append((xobj_n,
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (w, h, len(data)) +
            data + b"\nendstream"))
        objects.append((page_n,
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources << /XObject << /Im0 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_n, w, h, xobj_n, page_n)))
        content = b"q %d 0 0 %d 0 0 cm /Im0 Do Q" % (w, h)
        content_n = num; num += 1
        objects.append((content_n, b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"))
        # rewrite page to point at its own content object
        objects[-2] = (page_n,
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources << /XObject << /Im0 %d 0 R >> >> "
            b"/Contents %d 0 R >>" % (pages_n, w, h, xobj_n, content_n))
        page_nums.append(page_n)

    kids = " ".join(f"{p} 0 R" for p in page_nums).encode()
    objects.insert(0, (pages_n, b"<< /Type /Pages /Kids [ %s ] /Count %d >>" % (kids, len(page_nums))))
    objects.insert(0, (catalog_n, b"<< /Type /Catalog /Pages %d 0 R >>" % pages_n))

    info_n = num
    meta = ("(smartedu textbook downloader) /Producer (smartedu-dl server) /CreationDate (D:%s)"
            % time.strftime("%Y%m%d%H%M%S")).encode()
    objects.append((info_n, b"<< /Title (%s) >>" % titles[0].encode("utf-8", "ignore")))

    buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    # order objects by number
    for n, body in sorted(objects, key=lambda x: x[0]):
        offsets[n] = len(buf)
        buf += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref_off = len(buf)
    maxn = max(offsets)
    buf += b"xref\n0 %d\n" % (maxn + 1)
    buf += b"0000000000 65535 f \n"
    for n in range(1, maxn + 1):
        buf += b"%010d 00000 n \n" % offsets.get(n, 0)
    buf += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        maxn + 1, catalog_n, info_n, xref_off)
    with open(out_path, "wb") as f:
        f.write(bytes(buf))
    return {"bytes": len(buf), "pages": len(jpg_paths), "dims": img_dims}


def jpeg_size(data):
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
            h, w = int.from_bytes(data[i + 5:i + 7], "big"), int.from_bytes(data[i + 7:i + 9], "big")
            return w, h
        i += 2 + ln
    return 1488, 2104  # 兜底：实测转码图统一尺寸


def sanitize(name):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip() or "untitled"
    return name[:180]


def download_images(meta, dest_dir, job_id=None, pages=None):
    """路径 B：逐页 JPG（不发 Referer）→ 合成 PDF。pages=None 表示 1..pagesize。"""
    tpl = meta["images"]["template"]
    if not tpl:
        raise RuntimeError("详情 JSON 中没有 preview 模板，无法走图片路径")
    total = meta["images"]["pagesize"]
    plist = list(pages) if pages else list(range(1, total + 1))
    tmp = os.path.join(dest_dir, f"_pages_{job_id or uuid.uuid4().hex[:8]}")
    os.makedirs(tmp, exist_ok=True)
    done = {"n": 0, "bytes": 0}
    lock = threading.Lock()

    def fetch_page(p):
        url = tpl.replace("{page}", str(p))
        for i in range(3):
            u = rotate_host(url, i + p)
            st, body, _ = http_get(u, timeout=60)
            if st == 200 and body[:2] == b"\xff\xd8":
                path = os.path.join(tmp, f"{p:05d}.jpg")
                with open(path, "wb") as f:
                    f.write(body)
                with lock:
                    done["n"] += 1
                    done["bytes"] += len(body)
                    if job_id:
                        set_job(job_id, downloaded=done["n"], total=len(plist),
                                bytes=done["bytes"])
                return path
        raise RuntimeError(f"第 {p} 页下载失败 HTTP {st}")

    with ThreadPoolExecutor(max_workers=6) as ex:
        paths = list(ex.map(fetch_page, plist))

    out = os.path.join(dest_dir, sanitize(meta["title"]) + ".pdf")
    res = build_pdf_from_images(paths, out)
    # 边界校验：pagesize+1 页应 403（实测特征）
    boundary = None
    if not pages and total:
        st, _, _ = http_get(tpl.replace("{page}", str(total + 1)), timeout=15)
        boundary = st
    return {"path": out, "bytes": res["bytes"], "mode": "images", "pages": res["pages"],
            "boundary_status": boundary, "jpeg_total": done["bytes"]}


# ---------------------------------------------------------------------------
# 目录（catalog）：内置离线快照 + 在线刷新
# ---------------------------------------------------------------------------

CATALOG_FILES = ["part_100.json", "part_101.json", "part_102.json", "part_103.json"]
_catalog_cache = {"items": None, "mtime": 0}


def load_catalog(data_dir):
    items = []
    files = []
    cdir = os.path.join(data_dir, "catalog")
    if os.path.isdir(cdir):
        files += [os.path.join(cdir, f) for f in sorted(os.listdir(cdir)) if f.endswith(".json")]
    for f in CATALOG_FILES:
        p = os.path.join(data_dir, f)
        if os.path.exists(p):
            files.append(p)
    for p in files:
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            arr = data if isinstance(data, list) else (
                data.get("items") or data.get("Data") or [])
            for it in arr:
                if isinstance(it, dict) and it.get("id"):
                    items.append(it)
        except Exception as e:
            print(f"[catalog] skip {p}: {e}", file=sys.stderr)
    # dedupe by id
    seen, uniq = set(), []
    for it in items:
        if it["id"] not in seen:
            seen.add(it["id"])
            uniq.append(it)
    return uniq


def catalog_entry_shape(sample):
    """打印一条目录记录的结构，便于确认字段（调试用）。"""
    return json.dumps(sample, ensure_ascii=False)[:500]


def _entry_tags(labels):
    """把目录条目的 global_label.zh-CN 解析成维度字典。

    已验证条目形态：["zxx", "普通高中教科书 物理 选择性必修 第三册", "<tagid>", "x_document"]
    —— 目录条目本身不带学科/版本等维度标签，因此筛选主要依赖标题文本匹配；
    若快照条目带有形如 "zxxxk:物理" 的标签或 tag_list 字段，则自动启用维度分面。
    """
    tags = {}
    for l in labels:
        m = re.match(r"^(zxx[a-zA-Z_]*|tagView|channel):(.+)$", str(l))
        if m:
            tags[m.group(1)] = m.group(2)
    return tags


DIM_KEYS = {"stage": "学段", "subject": "学科", "grade": "年级", "volume": "册别",
            "version": "版本", "tagView": "类别"}


def summarize(items, q="", filters=None, offset=0, limit=50):
    filters = {k: v for k, v in (filters or {}).items() if v and v != "*"}
    ql = q.strip().lower()
    out, facets = [], {}
    for it in items:
        labels = [str(x) for x in ((it.get("global_label") or {}).get("zh-CN") or [])]
        title = (it.get("global_title") or {}).get("zh-CN") or it["id"]
        text = (" ".join(labels) + " " + title).lower()
        if ql and all(t not in text for t in [ql]):
            # 多关键词（空格分隔）需全部命中
            if not all(k in text for k in ql.split()):
                continue
        tags = _entry_tags(labels)
        ok = True
        for dim, val in filters.items():
            if dim in tags and tags[dim] != val:
                ok = False
                break
            if dim not in tags and val.lower() not in text:
                ok = False
                break
        if not ok:
            continue
        out.append({"id": it["id"], "title": title, "tags": tags,
                    "labels": labels, "sort": it.get("sort") or 0})
        for k, v in tags.items():
            facets.setdefault(k, {})
            facets[k][v] = facets[k].get(v, 0) + 1
    out.sort(key=lambda x: (-x["sort"], x["title"]))
    return {"total": len(out), "offset": offset, "limit": limit,
            "items": out[offset:offset + limit], "facets": facets}


# ---------------------------------------------------------------------------
# Job runner
# ---------------------------------------------------------------------------

def start_job(cid, mode="auto", pages=None):
    job_id = uuid.uuid4().hex[:12]
    job = {"job_id": job_id, "cid": cid, "mode": mode, "status": "running",
           "step": "fetch details json", "progress": 0, "downloaded": 0, "total": 0,
           "bytes": 0, "result": None, "error": None, "log": [], "created": time.time()}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def run():
        try:
            detail = fetch_details(cid)
            meta = parse_meta(detail)
            job["log"].append(f"title={meta['title']} pagesize={meta['pagesize']}")
            dest = os.path.join(DOWNLOAD_DIR, cid)
            os.makedirs(dest, exist_ok=True)
            set_job(job_id, step="downloading", meta=meta)

            modes = ["native", "images"] if mode == "auto" else [mode]
            last_err = None
            for m in modes:
                try:
                    if m == "native":
                        if not meta["native"]["public_url"]:
                            raise RuntimeError("无 source PDF 条目")
                        job["log"].append("try path A (native pdf)")
                        r = download_native(meta, dest, job_id)
                    else:
                        job["log"].append("try path B (images -> pdf)")
                        set_job(job_id, total=meta["pagesize"])
                        r = download_images(meta, dest, job_id, pages)
                    r["title"] = meta["title"]
                    r["fallback_used"] = (m == "images" and "native" in modes and len(job["log"]) > 2)
                    set_job(job_id, status="done", progress=100, result=r,
                            step=f"saved → {os.path.relpath(r['path'], HERE)}")
                    return
                except Exception as e:
                    last_err = str(e)
                    job["log"].append(f"path {m} failed: {last_err}")
                    set_job(job_id, step=f"path {m} failed → fallback")
            raise RuntimeError(last_err or "all paths failed")
        except Exception as e:
            set_job(job_id, status="error", error=str(e))

    threading.Thread(target=run, daemon=True).start()
    return job_id


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "SmarteduDL/1.0"
    data_dir = DATA_DIR_DEFAULT
    catalog = None

    # ---- utils -------------------------------------------------------------
    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data, ctype, fname=None, status=200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if fname:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + quote(fname))
        self.end_headers()
        self.wfile.write(data)

    def get_catalog(self):
        cls = Handler
        mt = 0
        for root, _, fs in os.walk(cls.data_dir):
            for f in fs:
                if f.endswith(".json"):
                    try:
                        mt = max(mt, os.path.getmtime(os.path.join(root, f)))
                    except OSError:
                        pass
        if cls.catalog is None or _catalog_cache["mtime"] != mt:
            items = load_catalog(cls.data_dir)
            cls.catalog = items
            _catalog_cache["mtime"] = mt
            print(f"[catalog] loaded {len(items)} entries")
        return cls.catalog

    # ---- routing -----------------------------------------------------------
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path, qs = parsed.path, parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                return self.serve_static("index.html")
            if path.startswith("/static/"):
                return self.serve_static(path[len("static/"):])
            if path == "/api/catalog":
                return self.api_catalog(qs)
            if path == "/api/detail":
                cid = qs.get("contentId", [""])[0]
                meta = parse_meta(fetch_details(cid))
                return self.send_json(meta)
            if path == "/api/job":
                jid = qs.get("id", [""])[0]
                with JOBS_LOCK:
                    j = JOBS.get(jid)
                    if not j:
                        return self.send_json({"error": "job not found"}, 404)
                    return self.send_json({k: v for k, v in j.items() if k != "meta"})
            if path == "/api/file":
                return self.serve_result_file(qs)
            if path == "/api/download":
                cid = qs.get("contentId", [""])[0]
                mode = qs.get("mode", ["auto"])[0]
                pages = None
                if "pages" in qs:
                    a, b = qs["pages"][0].split("-")
                    pages = list(range(int(a), int(b) + 1))
                return self.send_json({"jobId": start_job(cid, mode, pages)})
            if path == "/api/refresh":
                return self.send_json({"error": "请使用 refresh_catalog.py 从浏览器导出的 JSON 刷新目录",
                                       "hint": "python3 refresh_catalog.py --input ~/Downloads/tag_view_zxx.json"},
                                      400)
            return self.send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    # ---- handlers ----------------------------------------------------------
    def serve_static(self, rel):
        rel = rel.split("?")[0]
        fp = os.path.normpath(os.path.join(STATIC, rel))
        if not fp.startswith(STATIC) or not os.path.isfile(fp):
            return self.send_json({"error": f"missing static file {rel}"}, 404)
        ctype = {".html": "text/html;charset=utf-8", ".js": "text/javascript;charset=utf-8",
                 ".css": "text/css;charset=utf-8", ".svg": "image/svg+xml",
                 ".png": "image/png", ".ico": "image/x-icon"}.get(
            os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as f:
            self.send_bytes(f.read(), ctype)

    def api_catalog(self, qs):
        items = self.get_catalog()
        q = qs.get("q", [""])[0]
        limit = int(qs.get("limit", ["50"])[0])
        offset = int(qs.get("offset", ["0"])[0])
        tf = {k: qs[k][0] for k in DIM_KEYS if k in qs}
        res = summarize(items, q, tf, offset, limit)
        res["catalog_size"] = len(items)
        self.send_json(res)

    def serve_result_file(self, qs):
        jid = qs.get("job", [""])[0]
        with JOBS_LOCK:
            j = JOBS.get(jid)
        if not j or not j.get("result"):
            return self.send_json({"error": "job not ready"}, 404)
        p = j["result"]["path"]
        if not os.path.isfile(p):
            return self.send_json({"error": "file gone"}, 404)
        pack = qs.get("pack", [""])[0] == "zip"
        if pack:
            zp = p[:-4] + ".assets.zip"
            if not os.path.exists(zp):
                with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
                    z.write(p, os.path.basename(p))
                    d = os.path.dirname(p)
                    for f in sorted(os.listdir(d)):
                        if f.endswith(".jpg"):
                            z.write(os.path.join(d, f), "pages/" + f)
            p = zp
        with open(p, "rb") as f:
            data = f.read()
        self.send_bytes(data,
                        "application/zip" if p.endswith(".zip") else "application/pdf",
                        os.path.basename(p))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=DATA_DIR_DEFAULT)
    args = ap.parse_args()
    Handler.data_dir = args.data_dir
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(args.data_dir, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"smartedu 教材下载站 → http://{args.host}:{args.port}  (Ctrl+C 退出)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
