"""Cases — 决策案例 / 反例候选 + 晋升。

L1Item.type=case → case_candidates(sqlite),阈值达标晋升到 git cases/<slug>.md。
"""

from helper.cases.consumer import consume_case_items
from helper.cases.promoter import promote_eligible, promote_one

__all__ = ["consume_case_items", "promote_eligible", "promote_one"]
