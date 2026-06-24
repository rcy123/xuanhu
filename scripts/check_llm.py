"""模型网关连通检查脚本。

可通过 ``uv run python scripts/check_llm.py`` 执行。
输出只使用脱敏配置或简单状态，不得打印 MODEL_GATEWAY_API_KEY。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中，以便 uv run python scripts/check_llm.py 可执行
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# Windows 控制台 UTF-8 输出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> None:
    """执行模型网关连通性检查并输出结果。"""
    try:
        from app.core.config import get_settings
        from app.core.gateway import ModelGatewayClient
    except ImportError as e:
        print("=== 模型网关连通检查 ===")
        print("[FAIL] 配置加载失败：无法导入 app 模块")
        print(f"   错误: {e}")
        print("   请确保在项目根目录执行: uv run python scripts/check_llm.py")
        sys.exit(2)

    try:
        settings = get_settings()
    except Exception as e:
        print("=== 模型网关连通检查 ===")
        print("[FAIL] 配置加载失败：缺少必填环境变量")
        print(f"   错误类型: {type(e).__name__}")
        print("   请检查 .env 文件或环境变量是否包含：")
        print("     - DB_URL")
        print("     - REDIS_URL")
        print("     - MODEL_GATEWAY_BASE_URL")
        print("     - MODEL_GATEWAY_API_KEY")
        print("     - CHAT_MODEL")
        print("     - EMBEDDING_MODEL")
        print("     - EMBEDDING_DIM")
        sys.exit(2)

    # 脱敏输出配置信息
    safe_config = settings.safe_dump()
    print("=== 模型网关连通检查 ===")
    print(f"环境: {safe_config.get('app_env', 'unknown')}")
    print(f"网关地址: {safe_config.get('model_gateway_base_url', 'unknown')}")
    print(f"路由配置: {safe_config.get('model_gateway_route_profile', 'default')}")
    print(f"Chat 模型: {safe_config.get('chat_model', 'unknown')}")
    print(f"Embedding 模型: {safe_config.get('embedding_model', 'unknown')}")
    print(f"Embedding 维度: {safe_config.get('embedding_dim', 'unknown')}")
    # 不输出 API key — safe_dump 已将 api_key 字段替换为 ***
    print(f"API Key: {safe_config.get('model_gateway_api_key', '***')}")
    print()

    client = ModelGatewayClient(settings)

    try:
        checks = asyncio.run(client.health_check())
    except Exception as e:
        print("--- 检查结果 ---")
        print(f"[FAIL] 连通检查执行失败: {type(e).__name__}")
        sys.exit(2)

    print("--- 检查结果 ---")
    all_ok = True
    for name, status in checks.items():
        icon = "[OK]" if status == "ok" else "[FAIL]"
        print(f"  {icon} {name}: {status}")
        if status != "ok":
            all_ok = False

    overall = "ok" if all_ok else "degraded"
    print(f"\n整体状态: {overall}")

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
