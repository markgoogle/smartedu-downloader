"""集中配置：全部可用环境变量覆盖，带边界校验。"""
import os
import re
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _inti(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except ValueError:
        return default
    return max(lo, min(hi, v))


def _inff(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(name, default))
    except ValueError:
        return default
    return max(lo, min(hi, v))


@dataclass(frozen=True)
class Settings:
    host: str = os.environ.get("SMARTEDU_HOST", "127.0.0.1")
    port: int = _inti("SMARTEDU_PORT", 8000, 1, 65535)
    data_dir: str = os.environ.get("SMARTEDU_DATA_DIR", os.path.join(ROOT, "catalog_data"))
    download_dir: str = os.environ.get("SMARTEDU_DOWNLOAD_DIR", os.path.join(ROOT, "downloads"))
    static_dir: str = os.environ.get("SMARTEDU_STATIC_DIR", os.path.join(ROOT, "web", "dist"))
    # 资源与安全上限
    max_jobs: int = _inti("SMARTEDU_MAX_JOBS", 64, 1, 1024)
    page_concurrency: int = _inti("SMARTEDU_PAGE_CONCURRENCY", 6, 1, 16)
    image_pages_limit: int = _inti("SMARTEDU_IMAGE_PAGES_LIMIT", 500, 1, 2000)
    http_timeout: float = _inff("SMARTEDU_HTTP_TIMEOUT", 30.0, 5.0, 300.0)
    job_retain_seconds: int = _inti("SMARTEDU_JOB_RETAIN", 86400, 60, 7 * 86400)
    rate_limit_per_minute: int = _inti("SMARTEDU_RATE_LIMIT", 120, 0, 100000)
    allow_private_hosts: bool = os.environ.get("SMARTEDU_ALLOW_PRIVATE_HOSTS", "0") == "1"


settings = Settings()

# —— 平台端点（2026-11-01 实测验证）——
S_FILE_HOSTS = ["https://s-file-1", "https://s-file-2", "https://s-file-3"]
DETAIL_PATH_TMPL = "/zxx/ndrv2/resources/tch_material/details/{cid}.json"
ALLOWED_REMOTE_SUFFIXES = (".ykt.cbern.com.cn",)  # SSRF 白名单：仅平台 CDN 域
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# —— 登录态列表/搜索通道（可选，需 Cookie；匿名时 403/418）——
# 页面 https://basic.smartedu.cn/tchMaterial?defaultTag=<tagA>%2F<tagB>%2F<tagC>
# 背后的聚合检索接口（浏览器 Network 面板可复核）：
SEARCH_API_TEMPLATES = [
    # c: 逗号分隔的 tag id 组合（学段/学科/版本），支持分页翻页枚举全站教材
    ("search", "https://x-search.ykt.cbern.com.cn/v1/resources/aggregate"
               "?appCode=smart_edu&pageOffset={offset}&pageSize={size}"
               "&sort=asc&c={tags}"),
]
DEFAULT_TAGS = {
    # 用户提供的示例 defaultTag（高中 / 物理 / 沪科技版）
    "stage_senior": "e7bbcefe-0590-11ed-9c79-92fc3b3249d5",
    "subject_physics": "2a05962f-759e-43cc-bb1b-5340721fb6bc",
    "publisher_shkj": "44bed1a6-54e6-11ed-9c34-850ba61fa9f4",
}
