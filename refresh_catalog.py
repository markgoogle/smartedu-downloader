#!/usr/bin/env python3
"""刷新本地教材目录快照。

平台目录端点匿名不可达（实测 403），因此支持三种来源：
  1) --input FILE            浏览器/其它工具导出的 JSON（数组或含 items/Data/data/resources 键）
  2) --from-details DIR      扫描目录下的 *.json 详情文件，抽取元数据生成快照条目
  3) --ids FILE              contentId 列表（每行一个 UUID），在线逐个抓详情 JSON 构建快照

输出统一为 catalog_data/catalog.json：[{id, global_title, global_label, tag_list, sort}, ...]
用法示例：
  python3 refresh_catalog.py --ids ids.txt
  python3 refresh_catalog.py --from-details /path/to/details_dir
  python3 refresh_catalog.py --input ~/Downloads/tag_view_zxx.json
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DETAIL_TMPL = ("https://s-file-{i}.ykt.cbern.com.cn/zxx/ndrv2/resources/"
               "tch_material/details/{cid}.json")
UA = {"User-Agent": "Mozilla/5.0 (smartedu-catalog-refresh)"}


def fetch_detail(cid, timeout=20):
    last = None
    for i in (1, 2, 3):
        url = DETAIL_TMPL.format(i=i, cid=cid)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status == 200:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # HTTPError(403) 等
            last = e
        time.sleep(0.3)
    print(f"  ! {cid}: {last}", file=sys.stderr)
    return None


def slim(detail: dict) -> dict:
    """保留列表所需字段，去掉 ti_items 大对象，快照保持小体积。"""
    tags = [{"tag_dimension_id": t.get("tag_dimension_id"),
             "tag_name": t.get("tag_name")} for t in detail.get("tag_list") or []]
    return {"id": detail["id"],
            "global_title": detail.get("global_title"),
            "global_label": detail.get("global_label"),
            "tag_list": tags,
            "update_time": detail.get("update_time"),
            "sort": int(time.time())}


def extract_array(data):
    if isinstance(data, list):
        return data
    for k in ("items", "Data", "data", "resources", "list"):
        v = data.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for kk in ("resources", "list", "items"):
                if isinstance(v.get(kk), list):
                    return v[kk]
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="catalog_data/catalog.json")
    ap.add_argument("--input", help="外部导出的目录 JSON")
    ap.add_argument("--from-details", help="包含详情 *.json 的目录")
    ap.add_argument("--ids", help="contentId 列表文件（每行一个 UUID）")
    ap.add_argument("--delay", type=float, default=0.25, help="--ids 模式请求间隔秒")
    args = ap.parse_args()

    entries = []
    if args.input:
        with open(args.input, encoding="utf-8") as f:
            arr = extract_array(json.load(f))
        for it in arr:
            if isinstance(it, dict) and it.get("id"):
                entries.append(it)
        print(f"input: {len(entries)} 条")
    if args.from_details:
        n = 0
        for root, _, fs in os.walk(args.from_details):
            for f in sorted(fs):
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        d = json.load(fh)
                    if d.get("id") and d.get("ti_items"):
                        entries.append(slim(d))
                        n += 1
                except Exception as e:
                    print(f"  skip {f}: {e}", file=sys.stderr)
        print(f"details dir: {n} 条")
    if args.ids:
        with open(args.ids, encoding="utf-8") as f:
            cids = [x.strip().lower() for x in f
                    if UUID_RE.match((x.strip().lower()))]
        print(f"ids: {len(cids)} 个，在线抓取中…")
        ok = 0
        for i, cid in enumerate(cids):
            d = fetch_detail(cid)
            if d:
                entries.append(slim(d))
                ok += 1
            if i % 50 == 0:
                print(f"  {i}/{len(cids)} ok={ok}")
            time.sleep(max(0.0, args.delay))
        print(f"online: {ok}/{len(cids)} 成功")

    if not entries:
        print("没有可用条目，未写出。", file=sys.stderr)
        sys.exit(1)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    tmp = args.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"items": entries}, f, ensure_ascii=False)
    os.replace(tmp, args.out)
    size = os.path.getsize(args.out)
    print(f"✔ 写出 {args.out}：{len(entries)} 条，{size/1024:.0f} KB")
    print("  站点内可 POST /api/catalog/reload 或重启服务生效。")


if __name__ == "__main__":
    main()
