"""创建 / 更新医师登录账号（阶段 1 运维脚本）。

密码以 argon2id 哈希写入 ``doctors`` 表，禁止明文。用法：

    uv run python -m scripts.create_doctor --name 张三 --password '...' [--doctor-id <uuid>] [--disable]

- 不传 ``--doctor-id`` 时自动生成 UUID，输出到 stdout 供下发医师使用。
- ``--disable`` 停用账号（不影响已签发 token，新登录拒绝）。
- 该脚本直连数据库（读 ``DB_URL``），应只在受控运维环境执行。
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select

from app.api.auth import hash_password
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.doctor import Doctor


async def main() -> None:
    parser = argparse.ArgumentParser(description="创建/更新医师登录账号")
    parser.add_argument("--name", required=True, help="医师姓名")
    parser.add_argument("--password", required=True, help="登录密码（明文仅出现在命令行，落库为 argon2id 哈希）")
    parser.add_argument("--doctor-id", default=None, help="指定医师 UUID（可选，缺省自动生成）")
    parser.add_argument("--disable", action="store_true", help="停用该账号")
    args = parser.parse_args()

    get_settings()  # 触发配置加载，尽早暴露 DB_URL 缺失
    factory = get_session_factory()

    async with factory() as db:
        doctor: Doctor | None = None
        if args.doctor_id:
            try:
                doctor_uuid = uuid.UUID(args.doctor_id)
            except ValueError:
                raise SystemExit(f"非法 doctor-id: {args.doctor_id!r}（需为 UUID）") from None
            result = await db.execute(select(Doctor).where(Doctor.id == doctor_uuid))
            doctor = result.scalar_one_or_none()
            if doctor is None:
                doctor = Doctor(id=doctor_uuid, name=args.name, password_hash="", enabled=True)
                db.add(doctor)
        else:
            doctor = Doctor(name=args.name, password_hash="", enabled=True)
            db.add(doctor)
            await db.flush()

        assert doctor is not None
        doctor.name = args.name
        if not args.disable:
            doctor.password_hash = hash_password(args.password)
            doctor.enabled = True
        else:
            doctor.enabled = False
        await db.commit()
        print(f"doctor_id={doctor.id} name={doctor.name} enabled={doctor.enabled}")
        if args.disable:
            print("账号已停用（新登录将被拒绝）")
        else:
            print("账号就绪：将 doctor_id 与密码分发给医师，登录接口 POST /api/v1/auth/login")


if __name__ == "__main__":
    asyncio.run(main())
