#!/usr/bin/env bash
# ============================================================================
# 悬壶（Xuanhu）— 生产强凭据生成器（阶段 3 T3.2）
# ============================================================================
# 一键生成一组强随机凭据并输出到 stdout，供复制到生产 .env。
# 禁止直接写文件——避免误把密钥落磁盘；由运维复制进受控环境变量。
#
# 用法：
#   ./scripts/gen-secrets.sh [--include-jwt]
#   --include-jwt  额外生成 JWT_SIGNING_KEY（首次初始化必需）
# ============================================================================
set -euo pipefail

include_jwt=0
for arg in "$@"; do
  case "$arg" in
    --include-jwt) include_jwt=1 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

b64() { openssl rand -base64 24 | tr -d '\n'; }
b64_long() { openssl rand -base64 48 | tr -d '\n'; }
hex12() { openssl rand -hex 12; }

cat <<EOF
# ============ 悬壶生产凭据（由 gen-secrets.sh 生成，仅复制到生产 .env） ============
POSTGRES_USER=xuanhu
POSTGRES_PASSWORD=$(b64)
REDIS_PASSWORD=$(b64)
MINIO_ACCESS_KEY=$(hex12)
MINIO_SECRET_KEY=$(b64)
EOF

if [ "$include_jwt" -eq 1 ]; then
  cat <<EOF
JWT_SIGNING_KEY=$(b64_long)
EOF
fi

cat <<EOF
# 生成时间: $(date -u +%Y-%m-%dT%H:%M:%SZ)
# 提示: 已配置好中间件后运行 docker compose -f docker-compose.yml up -d
# 提示: 后端 API 的 DB_URL/REDIS_URL 需按上述凭据拼接（DB_URL 含 POSTGRES_PASSWORD）
# 提示: 该输出含密钥，请勿粘贴进聊天工具/CI 日志；复制后立即关闭终端记录
EOF
