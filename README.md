# smartedu 教材下载网站（v2）

在线浏览**国家中小学智慧教育平台**教材列表，并**一键下载** PDF。
后端 FastAPI（全异步）+ 前端 Preact/Vite（gzip 后约 13 KB），单进程即可运行，内置两条下载路径的自动回退与完整性校验——全部行为基于 2026-11-01 的真实 HTTP 实测（见根目录 `REPORT.md`）。

---

## 目录

- [功能特性](#功能特性)
- [架构与技术选型](#架构与技术选型)
- [项目结构](#项目结构)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [目录快照（列表数据来源）](#目录快照列表数据来源)
- [下载流程说明](#下载流程说明)
- [REST API](#rest-api)
- [配置项（环境变量）](#配置项环境变量)
- [安全设计](#安全设计)
- [生产部署](#生产部署)
- [测试与验证](#测试与验证)
- [已知限制与注意事项](#已知限制与注意事项)
- [常见问题 FAQ](#常见问题-faq)

---

## 功能特性

- **教材列表浏览与检索**：关键词搜索 + 学段/学科/年级/册次/版本五维分面过滤，分页展示。
- **一键下载**：三种模式
  - `auto`（默认）：先试路径 A（出版社原生矢量 PDF），403 时自动回退路径 B（逐页 JPG 合成 PDF）；
  - `native`：仅路径 A；
  - `images`：仅路径 B，可选只合成部分页（如 `1-5`）。
- **完整性校验**：字节数 == 平台声明 `ti_size`、`%PDF-` 魔数、尾部 `%%EOF`、页数 == `requirements.pagesize`、越界页应返回 403（结果 JSON 中回显证据）。
- **后台任务面板**：实时进度（路径 B 显示已完成页/总页）、取消、完成后提供 *下载* 与 *浏览器内预览* 链接。
- **详情弹窗**：显示真实页数 vs preview 条目数（二者常不相等！）、源文件名与上传时间戳、路径 A 命中预测（样本内 100% 精确的经验判据）。
- **零外部数据库**：目录为本地 JSON 快照，任务状态在内存 + 产物落盘，重启即清空任务（产物保留）。

## 架构与技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| Web 框架 | **FastAPI**（Pydantic v2 校验、自动参数约束） | 轻量、异步原生、生态成熟 |
| 服务器 | **uvicorn[standard]** | 单 worker 即可支撑（IO 密集，全异步） |
| HTTP 客户端 | **httpx**（AsyncClient） | 连接池复用、支持 Range 流式下载 |
| 前端 | **Preact + Vite** | 构建产物 JS 26.4 KB（gzip 11.1 KB）+ CSS 4.3 KB（gzip 1.6 KB），无运行时框架负担 |
| PDF 合成 | 自研 `/DCTDecode` 零重编码写入 | 直接嵌入原始 JPEG 字节，不引入 Pillow，体积/质量 1:1 |
| 存储 | 文件系统（`catalog_data/`、`downloads/`） | 无 DB，占用极小 |

```
浏览器 (Preact SPA, web/dist)
   │  /api/*  (同源，无 CORS 需求)
   ▼
FastAPI (app/main.py)
   ├─ security.py   SSRF 白名单 / 限流 / 路径校验
   ├─ catalog.py    本地目录快照检索
   ├─ jobs.py       asyncio 后台任务（A→B 回退、进度、回收）
   └─ downloader.py httpx → 平台 CDN（s-file-*.ykt.cbern.com.cn / r{1,2,3}-ndr*.ykt.cbern.com.cn）
```

## 项目结构

```
.
├── app/                    # 后端（FastAPI 应用包）
│   ├── main.py             # REST API + SPA 托管 + 安全头中间件
│   ├── config.py           # 集中配置（全部 SMARTEDU_* 环境变量可覆盖）
│   ├── security.py         # URL 白名单(SSRF)、文件名清洗、路径穿越防护、限流器
│   ├── downloader.py       # 详情 JSON / 路径 A / 路径 B 核心实现
│   ├── jobs.py             # 任务存储与后台执行（含 24h 自动回收）
│   └── catalog.py          # 目录快照加载、五维分面检索
├── web/                    # 前端源码（Vite + Preact）
│   ├── src/                # api.js / components.jsx / jobs.js / main.jsx / style.css
│   └── dist/               # 构建产物（已提交；改动 src 后需重新 build）
├── catalog_data/
│   └── catalog.json        # 目录快照（种子 2 本；用 refresh_catalog.py 扩充）
├── downloads/              # 下载产物（每书一个子目录 + result.json）
├── refresh_catalog.py      # 目录快照刷新工具（三种来源）
├── requirements.txt        # Python 依赖
├── server.py               # ⚠️ 第一代实现，已被 app/ 取代，仅作留档
├── static/                 # ⚠️ 第一代前端，同上
├── REPORT.md               # 平台行为的逐条实测验证报告
└── work/                   # 实测原始证据（抽样、逐页记录）
```

## 环境要求

- **Python ≥ 3.10**（使用了 `str | None` 联合类型语法；开发验证于 3.12）
- **Node.js ≥ 18**（仅在需要重新构建前端时使用；`web/dist` 已随仓库提供）
- 出网可达 `*.ykt.cbern.com.cn`（平台 CDN）

## 快速开始

```bash
# 1) 安装后端依赖
python3 -m venv .venv && . .venv/bin/activate     # 可选但推荐
pip install -r requirements.txt

# 2a) 直接运行（使用仓库自带的 web/dist 构建产物与种子目录）
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 2b) 或修改了前端源码后重新构建
cd web && npm install && npm run build && cd ..

# 3) 打开 http://127.0.0.1:8000
```

冒烟自检：

```bash
curl -s localhost:8000/api/health | python3 -m json.tool
# 期望 "ok": true，且 catalog.entries >= 1

curl -s "localhost:8000/api/detail?contentId=b2774427-227a-496b-805e-59b510c87ebe" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['title'], d['pagesize'])"
# 期望：普通高中教科书·物理选择性必修 第三册 129
```

## 目录快照（列表数据来源）

平台的**教材总目录端点匿名访问返回 403**（实测），因此列表数据来自本地快照 `catalog_data/catalog.json`，详情与下载则**始终走在线接口**（公开、免登录）。三种刷新方式：

```bash
# ① 从浏览器导出的目录 JSON（登录平台后在 Network 面板保存 list 响应）
python3 refresh_catalog.py --input exported_list.json

# ② 从一个包含 *.json 详情文件的目录批量导入
python3 refresh_catalog.py --from-details ./details_dump/

# ③ 给一批 contentId，在线逐个抓详情 JSON 生成快照（限速可调）
python3 refresh_catalog.py --ids ids.txt --delay 0.25

# 输出默认写 catalog_data/catalog.json（原子写出），随后 POST /api/catalog/reload 或重启生效
```

> 合法 UUID 均可通过 `/api/detail` 在线查询，不依赖快照；快照只影响“列表浏览”体验。

## 下载流程说明

对每个 `contentId`：

1. **详情 JSON**（公开、免登录、免 Token、免 Referer）：
   `GET https://s-file-{1|2|3}.ykt.cbern.com.cn/zxx/ndrv2/resources/tch_material/details/{cid}.json`
   取 `ti_items[]` 中 `ti_file_flag=="source" && ti_format=="pdf"` 的 `ti_storages[0]`（私有域直链）、`ti_size`（权威字节数）、image 条目的 `requirements.pagesize`（权威页数，**不是** preview 条目数）。
2. **路径 A（原生 PDF）**：主机名 `-ndr-private.` → `-ndr.`，GET（不发 Referer，支持 Range）。校验 `Content-Length==ti_size`、`%PDF-`、`%%EOF`、页数。
   命中率约 37%（其余 403 AccessDenied）→ **自动回退 B**。经验判据：源文件名尾部毫秒时间戳 ≥ 2026-08-01 时样本内 100% 命中（UI 会显示预测，但仍以实测为准）。
3. **路径 B（图片合成）**：由 `custom_properties.preview` 模板替换页码，并发下载 `1..pagesize` 页 JPG（1488×2104，免鉴权），边界校验第 `pagesize+1` 页应为 403，然后按页序将原始 JPEG 以 `/DCTDecode` XObject 零重编码合成 PDF。
4. 产物：`downloads/{cid}/{书名}.pdf` + `{书名}.result.json`（声明值、实际校验、所用路径、耗时、失败证据）。

关键坑（均已在本项目中规避，详见 REPORT.md）：**绝不发送 Referer**（带非白名单 Referer 会 403）；中文/空格文件名统一 `quote()` 一次编码；URL 中的转码时间戳每次从 JSON 现取，不硬编码；私有域 HEAD 200 不代表可下载（GET 一律 401）。

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 存活探针 + 目录规模 + 活跃任务数 |
| GET | `/api/catalog?q=&stage=&subject=&grade=&volume=&version=&offset=&limit=` | 快照检索/分面/分页（limit ≤ 200） |
| POST | `/api/catalog/reload` | 热重载快照文件 |
| GET | `/api/detail?contentId=<uuid>` | 在线详情（标题、页数、preview 数、A 命中预测等） |
| POST | `/api/download` | body `{"contentId":"...","mode":"auto\|native\|images","pages":"1-5"(可选)}` → `{"jobId":"..."}` |
| GET | `/api/jobs` · `/api/job?id=` | 任务列表 / 单任务状态与进度 |
| POST | `/api/job/cancel?id=` | 取消任务 |
| GET | `/api/file?job=<id>[&inline=1]` | 下载产物 PDF（`inline=1` 浏览器内预览） |
| GET | `/*` | SPA 静态托管（history 回退到 index.html） |

OpenAPI 文档端点（`/docs`）在生产默认关闭。

## 配置项（环境变量）

| 变量 | 默认 | 说明（均带边界钳制） |
|---|---|---|
| `SMARTEDU_HOST` / `SMARTEDU_PORT` | `127.0.0.1` / `8000` | 监听地址；对外暴露请置于反向代理之后 |
| `SMARTEDU_DATA_DIR` | `./catalog_data` | 目录快照位置 |
| `SMARTEDU_DOWNLOAD_DIR` | `./downloads` | 下载产物位置 |
| `SMARTEDU_STATIC_DIR` | `./web/dist` | 前端构建产物位置 |
| `SMARTEDU_MAX_JOBS` | `64` (1–1024) | 同时存在的任务上限，超出返回 429 |
| `SMARTEDU_PAGE_CONCURRENCY` | `6` (1–16) | 路径 B 逐页并发 |
| `SMARTEDU_IMAGE_PAGES_LIMIT` | `500` (1–2000) | 单次合成页数上限（DoS 防护） |
| `SMARTEDU_HTTP_TIMEOUT` | `30` s (5–300) | 上游请求超时 |
| `SMARTEDU_JOB_RETAIN` | `86400` s | 完成任务保留期，到期清理产物 |
| `SMARTEDU_RATE_LIMIT` | `120` /min/IP | 滑动窗口限流（0 = 关闭） |
| `SMARTEDU_ALLOW_PRIVATE_HOSTS` | `0` | 调试用：允许访问非白名单主机（生产保持 0） |

## 安全设计

- **SSRF**：所有出站 URL 强制 `https` + 主机必须匹配 `.ykt.cbern.com.cn` 白名单后缀；拒绝 IP 字面量与内网主机（除非显式开启调试开关）。
- **路径穿越**：任务产物只存相对路径；读取时 `sanitize_filename` + `is_safe_relpath` + `realpath` 前缀三重校验，锁定在 `downloads/{cid}/` 内。
- **输入校验**：contentId 必须是标准 UUID 正则；mode 枚举；pages 区间格式；分页/查询参数 Pydantic/Query 边界。
- **HTTP 安全头**：`Content-Security-Policy`（`script-src 'self'`，无 inline/unsafe-eval）、`X-Frame-Options: DENY`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Permissions-Policy`。
- **DoS 缓解**：IP 限流、任务数上限、页数上限、请求体 64 KB 上限、有界重试与超时、hash 资源一年 immutable 缓存。
- **信息最小化**：错误消息脱敏（不回显服务器绝对路径）；关闭 docs/openapi；默认仅绑定回环地址；前端不用 `innerHTML`，Preact 默认转义。
- **合规提醒**：本工具面向个人教学备课用途，请遵守平台用户协议与著作权法，勿用于批量爬取或再分发。

## 生产部署

```ini
# /etc/systemd/system/smartedu.service
[Unit]
Description=smartedu textbook downloader
After=network.target

[Service]
User=smartedu
WorkingDirectory=/opt/smartedu
Environment=SMARTEDU_HOST=127.0.0.1
Environment=SMARTEDU_PORT=8000
Environment=SMARTEDU_DOWNLOAD_DIR=/var/lib/smartedu/downloads
ExecStart=/opt/smartedu/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always

[Install]
WantedBy=multi-user.target
```

- **worker 数保持 1**：任务状态在进程内存，多 worker 会导致任务列表不一致（如需横向扩展，把 JobStore 换成 Redis 后再开多 worker）。
- 反向代理示例（nginx）：`client_max_body_size 64k; proxy_pass http://127.0.0.1:8000;` 并转发 `X-Forwarded-For`（限流按 IP 生效需正确取得客户端 IP）。
- TLS 由反代终结；`/var/lib/smartedu` 单独分区并设置配额（原生 PDF 单本可达 70 MB）。
- 监控：`GET /api/health` 作 liveness；`jobs.active` 作负载指标。

## 测试与验证

```bash
# 语法/导入检查
python3 -m py_compile app/*.py refresh_catalog.py
python3 -c "from app.main import app; print(len(app.routes), 'routes')"

# 端到端冒烟（需出网；路径 A 命中样本：物理书）
python3 - <<'EOF'
import asyncio, tempfile
from app.downloader import Client, get_meta, download_native
async def main():
    async with Client() as c:
        m = await get_meta("b2774427-227a-496b-805e-59b510c87ebe", c)
        assert m.pagesize == 129, m.pagesize
        r = await download_native(m, tempfile.mkdtemp(), c)
        print("native ok:", r.bytes, "== ti_size", m.native.size)
asyncio.run(main())
EOF
```

`tests/` 目录预留纯函数离线用例位（`parse_detail`、URL 白名单、路径校验），建议 CI 中 `pytest tests/` + `npm run build` 双门禁。

## 已知限制与注意事项

1. **列表是快照**：平台目录端点匿名 403，无法在线拉全量列表；请用 `refresh_catalog.py` 定期刷新（种子仅 2 本对照教材）。
2. **路径 A 命中率 ≈ 37%**（2026-11 抽样，教材口径 22/59，CI 26.1–50.0%）；未命中自动回退 B，属预期行为而非故障。
3. **任务状态不持久**：重启服务后任务列表清空，已完成的 PDF 仍在 `downloads/`，可在磁盘上直接取用。
4. **图片版无文字层**（0 字符、0 `/Font`），不可检索；要文字层必须路径 A 命中。
5. **体积倍数因书而异**（实测 0.53×–2.53×），不要假设图片版一定更大。
6. 平台可能随时变更端点/桶策略；本项目结论只对 2026-11-01 的实测行为负责，失效时先对照 `REPORT.md` 复核端点。

## 常见问题 FAQ

**Q：下载一直排队/429？**
任务数达到 `SMARTEDU_MAX_JOBS` 或触发 IP 限流；等待完成或调大上限（注意磁盘与带宽）。

**Q：路径 A 报 403？**
正常：该书源 PDF 未镜像进公开桶（约 63%），`auto` 模式会自动走路径 B；用 `native` 模式则会明确失败并附证据。

**Q：详情页显示的页数和阅读器不一样？**
以 `requirements.pagesize` 为准；`preview` 条目只是前若干页预览（实测两本书均为 49 条而真实 129/167 页）。

**Q：如何扩充教材列表？**
登录后在浏览器 Network 面板导出目录 JSON → `python3 refresh_catalog.py --input export.json` → `POST /api/catalog/reload`。

**Q：可以局域网/公网直接访问吗？**
改 `SMARTEDU_HOST=0.0.0.0` 可行，但请务必置于 HTTPS 反向代理与访问控制之后，并保持 `SMARTEDU_ALLOW_PRIVATE_HOSTS=0`。

## 登录 Cookie（可选，解锁列表通道）

平台页面 `https://basic.smartedu.cn/tchMaterial?defaultTag=…` 公开可见，但其背后的聚合检索接口
`x-search.ykt.cbern.com.cn/v1/resources/aggregate` 需要登录态。本站支持输入 Cookie 走该通道枚举全站教材：

- **设置**：前端「抓取目录」页粘贴浏览器完整 Cookie（F12 → Network → 任意请求 → Request Headers → Cookie），或环境变量 `SMARTEDU_COOKIE="k1=v1; k2=v2"`；API：`POST /api/session {"cookie": "…"}`。
- **抓取流程**：阶段0 带 Cookie 分页调列表接口（401/403→`unauthorized`，网关拦截→`unreachable`，均自动降级）→ 阶段1 种子扩散 → 阶段2 UUID 采样。状态见 `GET /api/crawl/status`（含 `searchStatus`）。
- **安全**：Cookie 仅存进程内存，绝不写盘/日志/API 回显（只回显脱敏摘要 names/count）；只附带在白名单平台域请求上；重启即失效。清除：`DELETE /api/session` 或界面按钮。

