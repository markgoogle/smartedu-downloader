"""本地教材目录：加载快照、检索、分面。

平台目录端点（列表 API）匿名不可达（实测 403），因此列表数据来自本地快照
`catalog_data/*.json`（浏览器导出或 refresh_catalog.py 生成）。
详情 JSON 则始终在线获取，保证下载链路是"实时"的。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("smartedu.catalog")

DIM_KEYS = {"stage": "学段", "subject": "学科", "grade": "年级",
            "volume": "册别", "version": "版本"}

# global_label 常见维度 id → 内部键（若快照带这些标签则启用精确分面）
_LABEL_DIMS = {"zxxxd": "stage", "zxxxk": "subject", "zxxnj": "grade",
               "zxxcc": "volume", "zxxbb": "version"}


@dataclass
class Entry:
    id: str
    title: str
    tags: dict = field(default_factory=dict)
    text: str = ""          # 小写全文（标题+标签），用于匹配
    sort: int = 0

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "tags": self.tags, "sort": self.sort}


def _parse_entry(raw: dict) -> Entry | None:
    cid = raw.get("id") or raw.get("contentId") or raw.get("content_id")
    if not cid or not isinstance(cid, str):
        return None
    title = ((raw.get("global_title") or {}).get("zh-CN")
             or raw.get("title") or cid)
    labels = [str(x) for x in ((raw.get("global_label") or {}).get("zh-CN") or [])]
    tags: dict[str, str] = {}
    # 形态1: "zxxxk:物理"；形态2: tag_list 维度
    for l in labels:
        m = re.match(r"^(zxx[a-zA-Z_]*):(.+)$", l)
        if m and m.group(1) in _LABEL_DIMS:
            tags[_LABEL_DIMS[m.group(1)]] = m.group(2)
    for t in raw.get("tag_list") or []:
        dim = _LABEL_DIMS.get(t.get("tag_dimension_id"))
        if dim and t.get("tag_name"):
            tags.setdefault(dim, t["tag_name"])
    # 形态3: 标题行 "普通高中教科书 物理 选择性必修 第三册"（已验证目录条目形态）
    if "subject" not in tags:
        for l in labels:
            parts = l.split()
            if len(parts) >= 2 and ("教科书" in parts[0] or "教材" in parts[0]):
                tags["subject"] = parts[1]
                if len(parts) >= 4:
                    tags.setdefault("volume", " ".join(parts[2:]))
                elif len(parts) == 3:
                    tags.setdefault("volume", parts[2])
                break
    stage = None
    if "学段" in tags:
        stage = tags["学段"]
    elif any("小学" in t for t in labels):
        stage = "小学"
    elif any("初中" in t or "义务教育" in t for t in labels):
        stage = "初中"
    elif any("高中" in t or "普通高中" in t for t in labels):
        stage = "高中"
    if stage:
        tags.setdefault("stage", stage)
    text = " ".join(labels + [title]).lower()
    return Entry(id=cid.strip().lower(), title=title, tags=tags, text=text,
                 sort=int(raw.get("sort") or 0))


class Catalog:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self._loaded_at = 0.0
        self._source_files: list[str] = []

    # ---- 加载 ---------------------------------------------------------------
    def reload(self) -> int:
        entries, files = [], []
        root = self.data_dir
        if os.path.isdir(root):
            for dirpath, _, fs in os.walk(root):
                for f in sorted(fs):
                    if not f.endswith(".json"):
                        continue
                    p = os.path.join(dirpath, f)
                    files.append(p)
                    try:
                        with open(p, encoding="utf-8") as fh:
                            data = json.load(fh)
                    except Exception as e:
                        log.warning("skip %s: %s", p, e)
                        continue
                    arr = (data if isinstance(data, list) else
                           data.get("items") or data.get("Data") or
                           data.get("data") or [])
                    if isinstance(arr, dict):
                        arr = arr.get("resources") or arr.get("list") or []
                    for it in arr:
                        if isinstance(it, dict):
                            en = _parse_entry(it)
                            if en:
                                entries.append(en)
        seen, uniq = set(), []
        for en in entries:
            if en.id not in seen:
                seen.add(en.id)
                uniq.append(en)
        uniq.sort(key=lambda x: (-x.sort, x.title))
        with self._lock:
            self._entries = uniq
            self._source_files = files
            self._loaded_at = time.time()
        log.info("catalog loaded: %d entries from %d files", len(uniq), len(files))
        return len(uniq)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def status(self) -> dict:
        with self._lock:
            return {"entries": len(self._entries),
                    "files": [os.path.relpath(f, self.data_dir) for f in self._source_files],
                    "loadedAt": self._loaded_at}

    # ---- 检索 ---------------------------------------------------------------
    def search(self, q: str = "", filters: dict | None = None,
               offset: int = 0, limit: int = 50) -> dict:
        with self._lock:
            entries = self._entries
        filters = {k: v for k, v in (filters or {}).items()
                   if v and k in DIM_KEYS}
        terms = [t for t in re.split(r"\s+", q.strip().lower()) if t]
        out, facets = [], {}
        for en in entries:
            if terms and not all(t in en.text for t in terms):
                continue
            ok = True
            for dim, val in filters.items():
                v = val.lower()
                tagv = (en.tags.get(dim) or "").lower()
                if tagv:
                    if tagv != v and v not in tagv and tagv not in v:
                        ok = False
                        break
                elif v not in en.text:
                    ok = False
                    break
            if not ok:
                continue
            out.append(en)
            for k, tv in en.tags.items():
                if k in DIM_KEYS:
                    facets.setdefault(k, {})
                    facets[k][tv] = facets[k].get(tv, 0) + 1
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        return {"total": len(out), "offset": offset, "limit": limit,
                "items": [e.public() for e in out[offset:offset + limit]],
                "facets": facets}

    def get_tags(self, cid: str) -> dict | None:
        with self._lock:
            for en in self._entries:
                if en.id == cid:
                    return en.tags
        return None
