"""药名标准化器。

依据 ``herbs`` 表的 ``name`` + ``aliases`` 构建别名→标准名映射，
将处方中可能为别名的药名统一映射到标准药名后，再进行规则匹配。

为支持纯函数测试（不依赖 ORM），本模块以 ``HerbAliasProvider`` Protocol
解耦对 ``Herb`` ORM 的依赖：引擎调用方负责把 ORM 行转成 (标准名, 别名列表)。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable


@runtime_checkable
class HerbAliasProvider(Protocol):
    """提供药名标准名与其别名列表的最小协议。"""

    @property
    def standard_name(self) -> str:
        """标准药名。"""
        ...

    @property
    def aliases(self) -> list[str]:
        """别名列表（可能为空）。"""
        ...


class HerbNormalizer:
    """药名标准化器。

    基于 (标准名, 别名列表) 集合构建 ``别名 -> 标准名`` 映射。
    调用 :meth:`normalize` 时，命中则返回标准名，否则原样返回。
    """

    def __init__(self, providers: Iterable[HerbAliasProvider] | None = None) -> None:
        self._alias_to_standard: dict[str, str] = {}
        if providers is not None:
            for p in providers:
                self.register(p)

    def register(self, provider: HerbAliasProvider) -> None:
        """登记一个药名及其别名。"""
        standard = provider.standard_name
        self._alias_to_standard[standard] = standard
        for alias in provider.aliases:
            if isinstance(alias, str) and alias:
                self._alias_to_standard[alias] = standard

    def normalize(self, herb_name: str) -> str:
        """将药名标准化。无法识别时返回原名（保守策略）。"""
        if not herb_name:
            return herb_name
        return self._alias_to_standard.get(herb_name, herb_name)

    def normalize_all(self, herb_names: Iterable[str]) -> list[str]:
        """批量标准化。"""
        return [self.normalize(h) for h in herb_names]

    def is_known(self, herb_name: str) -> bool:
        """是否在已登记的药名/别名集合中。"""
        return herb_name in self._alias_to_standard


__all__ = ["HerbAliasProvider", "HerbNormalizer"]
