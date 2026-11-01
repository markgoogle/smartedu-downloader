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
S_FILE_HOSTS = ["s-file-1", "s-file-2", "s-file-3"]
DETAIL_PATH_TMPL = "/zxx/ndrv2/resources/tch_material/details/{cid}.json"
ALLOWED_REMOTE_SUFFIXES = (".ykt.cbern.com.cn",)  # SSRF 白名单：仅平台 CDN 域
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
