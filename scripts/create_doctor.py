"""创建 / 更新受控账号（医师 / 管理员）运维脚本。

密码以 argon2id 哈希写入 ``doctors`` 表，禁止明文。优先使用
``--password-stdin``，避免密码出现在进程参数中；保留 ``--password`` 以兼容
既有受控自动化。用法：

    # 创建医师（默认角色；登录名用拼音/工号，密码从标准输入安全传入）
    $InitialPassword | uv run python -m scripts.create_doctor --username zhangsan --name 张三 --password-stdin

    # 创建首个管理员：仅限受控运维环境；系统没有默认管理员或默认密码
    $InitialPassword | uv run python -m scripts.create_doctor --username admin --name 管理员 --password-stdin --role admin

- 登录使用 ``username``（唯一、好记），``doctors.id`` 仍为内部 UUID。
- 不传 ``--doctor-id`` 时自动生成 UUID，输出到 stdout 供下发账号使用。
- 更新已有账号时 ``--username`` 可改名（需唯一）。
- ``--disable`` 停用账号并递增 ``auth_version``：新登录被拒，已签发 JWT
  也会在下一次认证开启态请求时立即失效。
- 角色或凭据状态发生变更时递增 ``auth_version``，使已签发 JWT 立即失效。
- 该脚本直连数据库（读 ``DB_URL``），应只在受控运维环境执行。
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from app.api.auth import hash_password
from app.core.auth import ACCOUNT_ROLES, PASSWORD_MIN_LENGTH, USERNAME_PATTERN
from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.doctor import Doctor


async def main() -> None:
    parser = argparse.ArgumentParser(description="创建/更新医师或管理员登录账号")
    parser.add_argument("--name", required=True, help="账号显示名称")
    password_source = parser.add_mutually_exclusive_group(required=True)
    password_source.add_argument(
        "--password",
        help="登录密码（兼容参数；优先使用 --password-stdin，避免进程参数暴露）",
    )
    password_source.add_argument(
        "--password-stdin",
        action="store_true",
        help="从管道或密钥管理器读取密码；交互终端会隐藏输入，且不写入进程参数",
    )
    parser.add_argument(
        "--username",
        default=None,
        help="登录名（拼音/工号，唯一）；新账号必填，更新已有账号时可选改名",
    )
    parser.add_argument("--doctor-id", default=None, help="指定医师 UUID（可选，缺省自动生成）")
    parser.add_argument("--disable", action="store_true", help="停用该账号")
    parser.add_argument(
        "--role",
        choices=sorted(ACCOUNT_ROLES),
        default=None,
        help="账号角色；新账号省略时为 doctor，更新已有账号时省略则保留原角色",
    )
    args = parser.parse_args()

    if args.password_stdin:
        # Pipelines and secret-manager stdin stay non-interactive.  If an
        # operator runs this in a TTY, use getpass rather than echoing a
        # credential back to the terminal.
        password = getpass.getpass("登录密码: ") if sys.stdin.isatty() else sys.stdin.readline().rstrip("\r\n")
    else:
        password = args.password
    assert password is not None

    if not args.disable and len(password) < PASSWORD_MIN_LENGTH:
        raise SystemExit(f"密码至少需要 {PASSWORD_MIN_LENGTH} 个字符")
    username = (args.username or "").strip()
    if username and not USERNAME_PATTERN.fullmatch(username):
        raise SystemExit("登录名须为 3-64 位字母/数字/._-，且以字母或数字开头")

    get_settings()  # 触发配置加载，尽早暴露 DB_URL 缺失
    factory = get_session_factory()

    async with factory() as db:
        doctor: Doctor | None = None
        is_new = False
        if args.doctor_id:
            try:
                doctor_uuid = uuid.UUID(args.doctor_id)
            except ValueError:
                raise SystemExit(f"非法 doctor-id: {args.doctor_id!r}（需为 UUID）") from None
            result = await db.execute(select(Doctor).where(Doctor.id == doctor_uuid))
            doctor = result.scalar_one_or_none()
            if doctor is None:
                if not username:
                    raise SystemExit("新建账号必须提供 --username")
                doctor = Doctor(
                    id=doctor_uuid,
                    username=username,
                    name=args.name,
                    password_hash="",
                    role=args.role or "doctor",
                    enabled=True,
                    auth_version=1,
                )
                db.add(doctor)
                is_new = True
        else:
            if not username:
                raise SystemExit("新建账号必须提供 --username")
            doctor = Doctor(
                username=username,
                name=args.name,
                password_hash="",
                role=args.role or "doctor",
                enabled=True,
                auth_version=1,
            )
            db.add(doctor)
            await db.flush()
            is_new = True

        assert doctor is not None
        if username and doctor.username != username:
            # 改登录名前确认不冲突；唯一约束是最终防线。
            clash = await db.scalar(select(Doctor).where(Doctor.username == username))
            if clash is not None and clash.id != doctor.id:
                raise SystemExit(f"登录名 {username!r} 已被其他账号使用")
            doctor.username = username

        assert doctor is not None
        previous_role = doctor.role
        previous_enabled = doctor.enabled
        doctor.name = args.name
        if args.role is not None:
            doctor.role = args.role
        if not args.disable:
            doctor.password_hash = hash_password(password)
            doctor.enabled = True
        else:
            doctor.enabled = False
        if not is_new and (not args.disable or doctor.role != previous_role or doctor.enabled != previous_enabled):
            # Password replacement, enabling/disabling, and role changes all
            # invalidate every issued token.  A no-op repeat --disable keeps
            # its version stable and remains operationally idempotent.
            doctor.auth_version += 1
        await db.commit()
        print(
            f"doctor_id={doctor.id} username={doctor.username} name={doctor.name} "
            f"role={doctor.role} enabled={doctor.enabled} auth_version={doctor.auth_version}"
        )
        if args.disable:
            print("账号已停用（新登录将被拒绝）")
        else:
            print(f"账号就绪：登录名 {doctor.username}，请连同密码通过受控渠道分发。")
            if doctor.role == "admin":
                print("管理员账号仅可由本受控运维脚本创建；管理 API 不提供创建管理员的能力。")


if __name__ == "__main__":
    asyncio.run(main())
