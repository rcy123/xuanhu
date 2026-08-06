"""实体名索引（轻量级，用于 L3 运行时关联预热）。

从 ``knowledge_chunks`` 表加载所有 herb + formula 的 title，构建一个内存集合，
支持 O(1) 查找和 O(n) 子串匹配（n = 实体数，467 条，足够快）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("xuanhu.entity_index")


class _EntityIndex:
    """进程级单例实体名索引。

    ``_titles`` 是已加载的 herb + formula title 集合（按字符数从长到短排序），
    用于最长子串匹配——优先匹配"川芎茶调散"而非"川芎"。
    """

    _titles: list[str] = []
    _search_titles: list[tuple[str, str]] = []  # [(lower_title, original_title), ...] 排序后

    def load(self, titles: list[str]) -> None:
        """加载实体名列表（通常在 lifespan startup 或预热脚本中调用）。

        Args:
            titles: herb + formula 的 title 列表。
        """
        unique = list(dict.fromkeys(titles))  # 去重保序
        pairs = [(t.lower(), t) for t in unique]
        # 按长度降序排列：优先匹配最长的实体名
        pairs.sort(key=lambda x: len(x[1]), reverse=True)
        self._titles = unique
        self._search_titles = pairs

    @property
    def entity_count(self) -> int:
        return len(self._titles)

    def extract_entity(self, query: str) -> str | None:
        """从查询文本中提取已知实体名（最长子串匹配）。

        Args:
            query: 用户/RAG 查询文本。

        Returns:
            匹配到的原始实体名，无匹配时返回 None。
        """
        if not self._search_titles or not query:
            return None
        query_lower = query.lower()
        for lower_title, original_title in self._search_titles:
            if lower_title in query_lower:
                return original_title
        return None


# 进程级单例
_entity_index = _EntityIndex()


def get_entity_index() -> _EntityIndex:
    """获取进程级实体名索引单例。"""
    return _entity_index
