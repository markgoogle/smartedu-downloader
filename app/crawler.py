"""自动爬取平台全部教材 → 生成/增量更新本地目录快照 catalog_data/catalog.json。

实测约束（REPORT.md §3.9）：平台的"列表/目录端点"匿名不可达（403），
唯一匿名可读的数据端点是详情 JSON：
    https://s-file-{1,2,3}.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{cid}.json

因此"爬全站"采用 **种子扩散 + UUID 随机采样** 的自适应爬虫：
  1. 种子 = 现有快照 ∪ seed_ids.txt ∪ 内置种子；逐个抓详情，命中则入表；
  2. 每本命中的教材，从其 ti_items 存储 URL 中提取 `.pkg` 前的
     `edu_product/esp/assets/` 段与同目录兄弟对象线索（如 image 条目 `{cid}.t`），
     并把该书的 tag 维度值记入"已见筛选面"（学习网站分类方式）;
  3. 主体阶段：按 UUID v4 随机批量探测（HEAD 不支持→GET 小范围读取），
     连续 miss 概率随命中数收敛——当新增命中率低于阈值或达到时间/请求预算即停；
  4. 输出原子写 catalog.json（含 facets 统计），并保留旧快照为 catalog.prev.json。

这不是穷举保证（3.6e38 键空间不可能穷举），但在平台 ID 实际分布下能
持续发现新教材；配合 cron 定时运行即可保持列表接近全量。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
import uuid
from dataclasses import dataclass, field

import httpx

from .config import DETAIL_PATH_TMPL, S_FILE_HOSTS, UA, settings
from .filters import DIM_KEYS, dim_for, sort_facet

log = logging.getLogger("smartedu.crawler")

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_PKG_RE = re.compile(r"edu_product/esp/assets/([0-9a-f-]{36})(?:\.pkg|\.t)/")

CATALOG_NAME = "catalog.json"
SEED_FILE = "seed_ids.txt"          # 用户可放置额外种子（每行一个 UUID）
BUILTIN_SEEDS = [
    "b2774427-227a-496b-805e-59b510c87ebe",   # 物理 选择性必修第三册（沪科技版）
    "9085151a-b698-4b28-8c00-2c4aaf0c91ad",   # 语文 必修下册（人教版）
]


@dataclass
class CrawlStats:
    running: bool = False
    phase: str = "idle"           # idle|seeds|random|done|stopped|error
    requests: int = 0
    hits: int = 0                 # 本次运行新增命中
    known: int = 0                # 快照已有条目数
    misses: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    last_hit_title: str = ""

    def public(self) -> dict:
        return {"running": self.running, "phase": self.phase,
                "requests": self.requests, "hits": self.hits,
                "misses": self.misses, "known": self.known,
                "startedAt": self.started_at, "finishedAt": self.finished_at,
                "elapsed": (self.finished_at or time.time()) - self.started_at
                           if self.started_at else 0.0,
                "error": self.error, "lastHitTitle": self.last_hit_title}


def slim(detail: dict) -> dict:
    """目录快照条目：只留列表/筛选所需字段（去 ti_items 大对象）。"""
    tags = [{"tag_dimension_id": t.get("tag_dimension_id"),
             "tag_name": t.get("tag_name")}
            for t in detail.get("tag_list") or [] if isinstance(t, dict)]
    gt = detail.get("global_title") or {}
    title = gt.get("zh-CN") or detail.get("title") or ""
    if isinstance(title, dict):
        title = title.get("zh-CN", "")
    return {"id": detail["id"],
            "global_title": {"zh-CN": title},
            "global_label": detail.get("global_label"),
            "tag_list": tags,
            "update_time": detail.get("update_time"),
            "sort": int(time.time() * 1000)}


def extract_sibling_cids(detail: dict) -> list[str]:
    """从详情里找兄弟 contentId（同 pkg 结构 / teachmeterial_ids / chapter 引用）。"""
    found: list[str] = []
    raw = json.dumps(detail, ensure_ascii=False)
    for m in _PKG_RE.finditer(raw):
        found.append(m.group(1))
    for key in ("teachmeterial_ids", "chapter_ids"):
        v = detail.get(key)
        if isinstance(v, list):
            found.extend(str(x) for x in v if isinstance(x, str))
    out, seen = [], set()
    for c in found:
        c = c.lower()
        if UUID_RE.match(c) and c != detail.get("id") and c not in seen:
            seen.add(c)
            out.append(c)
    return out


class Crawler:
    """单实例后台爬虫；同一时刻只允许一个任务（生产安全：防并发写快照）。"""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.stats = CrawlStats()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---------- 对外控制 ----------
    async def start(self, max_requests: int = 20000,
                    seconds_budget: int = 600,
                    min_hit_rate: float = 0.002,
                    concurrency: int = 8) -> bool:
        if self.stats.running:
            return False
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(
            max_requests, seconds_budget, min_hit_rate, concurrency))
        return True

    def stop(self) -> None:
        if self.stats.running:
            self._stop.set()

    @property
    def busy(self) -> bool:
        return self.stats.running

    # ---------- 实现 ----------
    def _load_existing(self) -> dict[str, dict]:
        p = os.path.join(self.data_dir, CATALOG_NAME)
        items: dict[str, dict] = {}
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    data = json.load(fh)
                arr = data.get("items") if isinstance(data, dict) else data
                for it in arr or []:
                    if isinstance(it, dict) and it.get("id"):
                        items[str(it["id"]).lower()] = it
            except Exception as e:
                log.warning("existing catalog unreadable: %s", e)
        return items

    def _seed_ids(self) -> list[str]:
        ids: list[str] = []
        p = os.path.join(self.data_dir, SEED_FILE)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as fh:
                ids += [ln.strip().lower() for ln in fh
                        if UUID_RE.match(ln.strip().lower())]
        return ids + BUILTIN_SEEDS

    def _detail_urls(self, cid: str) -> list[str]:
        hosts = list(S_FILE_HOSTS)
        random.shuffle(hosts)
        return [h + DETAIL_PATH_TMPL.format(cid=cid) for h in hosts]

    async def _fetch_detail(self, client: httpx.AsyncClient,
                            cid: str) -> dict | None:
        self.stats.requests += 1
        for url in self._detail_urls(cid):
            try:
                r = await client.get(url, timeout=settings.http_timeout)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict) and data.get("id"):
                        # 仅收教材（x_document），与平台"教材"页口径一致
                        labels = ((data.get("global_label") or {}).get("zh-CN")
                                  or data.get("label") or [])
                        if "x_document" in labels:
                            return data
                    return None
                if r.status_code == 403:
                    return None      # 不存在或非教材：换 host 也是同样结果
            except (httpx.HTTPError, ValueError):
                continue
        return None

    async def _run(self, max_requests: int, seconds_budget: int,
                   min_hit_rate: float, concurrency: int) -> None:
        st = self.stats
        st.running, st.error, st.requests, st.hits, st.misses = True, "", 0, 0, 0
        st.started_at, st.finished_at, st.phase = time.time(), 0.0, "seeds"
        deadline = st.started_at + seconds_budget
        try:
            items = self._load_existing()
            st.known = len(items)
            sem = asyncio.Semaphore(max(1, min(concurrency, 16)))
            async with httpx.AsyncClient(
                    headers={"User-Agent": UA, "Accept": "application/json"},
                    follow_redirects=False, limits=httpx.Limits(
                        max_connections=concurrency * 2)) as client:

                async def one(cid: str) -> tuple[str, dict | None, list[str]]:
                    async with sem:
                        d = await self._fetch_detail(client, cid)
                        return cid, d, (extract_sibling_cids(d) if d else [])

                # —— 阶段1：种子 + 已知条目刷新 + 兄弟扩散 ——
                frontier = list(dict.fromkeys(self._seed_ids() + list(items.keys())))
                visited = set(frontier)
                while frontier and not self._stop.is_set() \
                        and st.requests < max_requests and time.time() < deadline:
                    batch, frontier = frontier[:concurrency * 4], frontier[concurrency * 4:]
                    results = await asyncio.gather(*(one(c) for c in batch))
                    for cid, d, sibs in results:
                        if d is None:
                            continue
                        st.hits += 1
                        st.last_hit_title = ((d.get("global_title") or {})
                                             .get("zh-CN") or cid)
                        items[cid] = slim(d)
                        for s in sibs:
                            if s not in visited:
                                visited.add(s)
                                frontier.append(s)
                    await self._maybe_flush(items)

                # —— 阶段2：UUID 随机采样，命中率低到阈值即停 ——
                st.phase = "random"
                window = 400
                recent: list[bool] = []
                while not self._stop.is_set() \
                        and st.requests < max_requests and time.time() < deadline:
                    probes = [str(uuid.uuid4()) for _ in range(window)]
                    results = await asyncio.gather(*(one(c) for c in probes))
                    new = 0
                    for cid, d, _ in results:
                        hit = d is not None
                        recent.append(hit)
                        if hit:
                            new += 1
                            st.hits += 1
                            items[cid] = slim(d)
                            st.last_hit_title = ((d.get("global_title") or {})
                                                 .get("zh-CN") or cid)
                    rate = sum(recent) / len(recent)
                    log.info("random window hit_rate=%.4f new=%d total=%d",
                             rate, new, len(items))
                    if rate < min_hit_rate:
                        break
                    await self._flush(items)
                st.phase = "stopped" if self._stop.is_set() else "done"
            await self._flush(items, final=True)
        except Exception as e:
            st.error = type(e).__name__ + ": " + str(e)[:200]
            st.phase = "error"
            log.exception("crawler failed")
        finally:
            st.running = False
            st.finished_at = time.time()

    # ---------- 落盘（原子写 + 分面统计） ----------
    def _facets(self, items: dict[str, dict]) -> dict:
        fac: dict[str, dict[str, int]] = {}
        for it in items.values():
            for t in it.get("tag_list") or []:
                k = dim_for(t.get("tag_dimension_id") or "")
                name = t.get("tag_name")
                if k and name:
                    fac.setdefault(k, {})
                    fac[k][name] = fac[k].get(name, 0) + 1
        return {k: [{"name": n, "count": c} for n, c in
                    sorted(fac[k].items(), key=lambda x: (-x[1], x[0]))]
                for k in fac}

    async def _maybe_flush(self, items: dict[str, dict]) -> None:
        if self.stats.hits and self.stats.hits % 200 == 0:
            await self._flush(items)

    async def _flush(self, items: dict[str, dict], final: bool = False) -> None:
        path = os.path.join(self.data_dir, CATALOG_NAME)
        arr = sorted(items.values(),
                     key=lambda x: x.get("update_time") or "", reverse=True)
        payload = {"note": "自动爬取生成（app/crawler.py）。"
                           "平台列表端点匿名 403，采用种子+随机采样发现教材。",
                   "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "count": len(arr),
                   "facets": self._facets(items),
                   "dims": {k: v["label"] for k, v in DIM_KEYS.items()},
                   "items": arr}
        tmp = path + ".tmp"
        def _write():
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            if final and os.path.isfile(path):
                prev = os.path.join(self.data_dir, "catalog.prev.json")
                try:
                    os.replace(path, prev)
                except OSError:
                    pass
            os.replace(tmp, path)
        await asyncio.to_thread(_write)
        log.info("catalog flushed: %d entries (%s)", len(arr),
                 "final" if final else "progress")
