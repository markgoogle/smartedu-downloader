"""平台筛选方式（学习自 basic.smartedu.cn/tchMaterial 页面）。

网站教材页的筛选条由 5 个标签维度组成，对应 tag_list 中的 tag_dimension_id：

  zxxxd 学段   (小学/初中/高中)          —— 页面第一组下拉
  zxxnj 年级   (一年级..高三)             —— 随学段联动
  zxxxk 学科   (语文/数学/物理…)          —— 页面第二组下拉
  zxxbb 版本   (人教版/沪科技版…)         —— 页面第三组下拉
  zxxcc 册别   (必修 第一册/统编…选择性必修)—— 页面第四组下拉

排序沿用平台目录的 sort 字段（平台默认按学段→学科→年级→册别的编排顺序）。
本模块把"每个维度有哪些值、以什么顺序展示"沉淀为可导出的筛选配置，
前端直接渲染，无需硬编码。
"""
from __future__ import annotations

# tag_dimension_id → 内部键 / 中文名 / 展示顺序（与网站筛选条一致）
DIM_ORDER = ["stage", "grade", "subject", "version", "volume"]
DIM_KEYS = {
    "stage":   {"dim": "zxxxd", "label": "学段"},
    "grade":   {"dim": "zxxnj", "label": "年级"},
    "subject": {"dim": "zxxxk", "label": "学科"},
    "version": {"dim": "zxxbb", "label": "教材版本"},
    "volume":  {"dim": "zxxcc", "label": "册别"},
}
_DIM_TO_KEY = {v["dim"]: k for k, v in DIM_KEYS.items()}

# 学段固定序（网站顺序）；其余维度按"常见值优先 + 字典序"稳定排序
STAGE_PRIORITY = ["小学", "初中", "高中", "职教", "高等教育"]


def dim_for(tag_dimension_id: str) -> str | None:
    return _DIM_TO_KEY.get(tag_dimension_id)


def sort_facet(key: str, values: list[str]) -> list[str]:
    vals = sorted(set(values))
    if key == "stage":
        def rank(v: str) -> tuple[int, str]:
            for i, p in enumerate(STAGE_PRIORITY):
                if v.startswith(p):
                    return (i, v)
            return (len(STAGE_PRIORITY), v)
        return sorted(vals, key=rank)
    if key == "grade":
        order = ["学前", "一年级", "二年级", "三年级", "四年级", "五年级",
                 "六年级", "七年级", "八年级", "九年级",
                 "高一", "高二", "高三", "高中年级", "义务教育", "普通高中"]
        def grank(v: str) -> tuple[int, int, str]:
            base = (order.index(v) + 1) if v in order else len(order) + 1
            # 小学/初中/高中前缀加权，保证跨学段年级不串序
            prefix = 0 if not v.startswith(("小", "初", "高")) else \
                {"小": 0, "初": 1, "高": 2}[v[0]]
            return (prefix, base, v)
        return sorted(vals, key=grank)
    if key == "subject":
        order = ["语文", "数学", "英语", "道德与法治", "思想政治", "历史",
                 "地理", "科学", "物理", "化学", "生物学", "体育与健康",
                 "音乐", "美术", "艺术", "书法", "信息技术", "通用技术",
                 "日语", "俄语", "西班牙语", "德语", "法语"]
        srank = lambda v: ((order.index(v) + 1) if v in order else len(order) + 1, v)
        return sorted(vals, key=srank)
    if key == "volume":
        def vrank(v: str) -> tuple[int, str]:
            for i, p in enumerate(["必修", "选择性必修", "选修", "全一册"]):
                if v.startswith(p):
                    return (i, v)
            return (len(["必修", "选择性必修", "选修", "全一册"]), v)
        return sorted(vals, key=vrank)
    return vals
