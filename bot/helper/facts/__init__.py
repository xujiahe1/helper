"""Facts — 决策性事实(主谓宾)候选 + 晋升。

L1Item.type=fact → fact_candidates(sqlite),阈值达标晋升到 git facts/<slug>.md。
"""

from helper.facts.consumer import consume_fact_items
from helper.facts.promoter import promote_eligible, promote_one

__all__ = ["consume_fact_items", "promote_eligible", "promote_one"]
