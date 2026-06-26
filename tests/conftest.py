"""pytest 会话级配置。

在测试收集阶段注入必需的默认环境变量，避免模块级 Settings()
导入时因缺少必填配置而抛出 ValidationError。

各测试文件如需验证"缺失必填配置"路径，可在用例内通过
monkeypatch.delenv 清除默认值后创建临时 Settings 实例。
"""

import os


def _set_test_defaults() -> None:
    """设置测试用默认环境变量（不覆盖已存在的值）。"""
    os.environ.setdefault("DB_URL", "postgresql://test:test@localhost:5432/xuanhu_test")
    os.environ.setdefault("REDIS_URL", "redis://:xuanhu_dev@localhost:6379/0")
    os.environ.setdefault("MODEL_GATEWAY_BASE_URL", "http://localhost:8080/v1")
    os.environ.setdefault("MODEL_GATEWAY_API_KEY", "sk-test-placeholder")
    os.environ.setdefault("CHAT_MODEL", "test-chat-model")
    os.environ.setdefault("EMBEDDING_MODEL", "test-embedding-model")
    os.environ.setdefault("EMBEDDING_DIM", "768")


_set_test_defaults()
