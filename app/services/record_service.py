"""病历服务层。

P7-3 实现：
- GET record（latest / 指定 version）
- PUT record（版本化编辑，新增 version 不覆盖旧版本）
- GET export（txt / json / md，即时生成不落盘）
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    InvalidStageTransitionError,
    InvalidStateVersionError,
    SessionNotFoundError,
)
from app.core.exceptions import (
    ValidationError as XuanhuValidationError,
)
from app.models.audit import AuditEvent
from app.models.consult import ConsultSession
from app.models.review import MedicalRecord
from app.schemas.record import (
    RecordResponse,
    RecordUpdateRequest,
    RecordUpdateResponse,
)
from app.services.session_lock import SessionLock

logger = logging.getLogger("xuanhu.record")

# 支持的导出格式
_SUPPORTED_EXPORT_FORMATS = {"txt", "json", "md"}

# 允许编辑的阶段
_EDITABLE_STAGES = {"record", "done"}


def _now() -> datetime:
    """返回当前 UTC 时间（naive，与模型列类型保持一致）。"""
    return datetime.now(UTC).replace(tzinfo=None)


def _audit_event(
    session_id: uuid.UUID,
    event_type: str,
    actor_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
    trace_id: str,
) -> AuditEvent:
    """构造审计事件记录。"""
    return AuditEvent(
        session_id=session_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload,
        trace_id=trace_id,
    )


def _compute_diff(
    old_text: str,
    old_json: dict[str, Any],
    new_text: str | None,
    new_json: dict[str, Any] | None,
) -> dict[str, Any]:
    """计算与上一版本的差异摘要。

    不记录完整病历全文，仅记录变更字段名和变更类型。
    """
    diff: dict[str, Any] = {"changed_fields": []}
    if new_text is not None and new_text != old_text:
        diff["record_text"] = "modified"
        diff["changed_fields"].append("record_text")
    if new_json is not None and new_json != old_json:
        diff["record_json"] = "modified"
        # 提取 JSON 中变更的顶级 key 名
        old_keys = set(old_json.keys())
        new_keys = set(new_json.keys())
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        common = old_keys & new_keys
        changed = sorted(k for k in common if old_json.get(k) != new_json.get(k))
        if added or removed or changed:
            diff["record_json_changed_keys"] = {
                "added": added,
                "removed": removed,
                "changed": changed,
            }
            diff["changed_fields"].append("record_json")
    return diff


class RecordNotFoundError(SessionNotFoundError):
    """病历尚未生成。"""

    code = "RECORD_NOT_FOUND"
    message = "病历尚未生成"
    status_code = 404
    retryable = False


class ExportFormatUnsupportedError(XuanhuValidationError):
    """不支持的导出格式。"""

    code = "EXPORT_FORMAT_UNSUPPORTED"
    message = "不支持的导出格式，仅支持 txt / json / md"
    status_code = 400
    retryable = False


class RecordService:
    """病历应用服务。"""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # GET record
    # ------------------------------------------------------------------

    async def get_record(
        self,
        session_id: str,
        *,
        version: str | None = None,
        trace_id: str,
    ) -> RecordResponse:
        """获取病历（latest 或指定 version）。

        Args:
            session_id: 会话 ID。
            version: "latest" 或整数字符串，默认 "latest"。
            trace_id: 追踪 ID。

        Returns:
            RecordResponse。

        Raises:
            SessionNotFoundError: 会话不存在。
            RecordNotFoundError: 病历不存在。
        """
        sid = self._parse_session_id(session_id)

        # 校验会话存在
        await self._ensure_session_exists(sid)

        # 解析 version 参数
        version_int = self._resolve_version(version)

        # 查询病历
        record = await self._fetch_record(sid, version_int)
        if record is None:
            raise RecordNotFoundError(
                detail=(
                    f"session_id={session_id} version={version or 'latest'}"
                    " 无对应病历记录"
                ),
                retryable=False,
            )

        return RecordResponse(
            id=str(record.id),
            session_id=str(record.session_id),
            version=record.version,
            record_text=record.record_text,
            record_json=record.record_json,
            disclaimer=record.disclaimer,
            edited_by_doctor=record.edited_by_doctor,
            doctor_review_id=(
                str(record.doctor_review_id) if record.doctor_review_id else None
            ),
            diff_from_previous=record.diff_from_previous,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    # ------------------------------------------------------------------
    # PUT record
    # ------------------------------------------------------------------

    async def update_record(
        self,
        session_id: str,
        request: RecordUpdateRequest,
        *,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None = None,
    ) -> RecordUpdateResponse:
        """医师编辑病历，新增版本不覆盖旧版本。

        Args:
            session_id: 会话 ID。
            request: 编辑请求体。
            doctor_id: 医师标识。
            trace_id: 追踪 ID。
            x_state_version: 客户端 state_version。

        Returns:
            RecordUpdateResponse。

        Raises:
            SessionNotFoundError: 会话不存在。
            InvalidStageTransitionError: 当前阶段不允许编辑。
            InvalidStateVersionError: state_version 冲突。
            RecordNotFoundError: 无现有病历作为基准。
            SessionBusyError: 会话锁冲突。
        """
        sid = self._parse_session_id(session_id)

        lock = SessionLock(self._db, session_id, trace_id)
        await lock.acquire()
        try:
            return await self._update_record_locked(
                sid, session_id, request, doctor_id, trace_id, x_state_version
            )
        finally:
            await lock.release()

    async def _update_record_locked(
        self,
        sid: uuid.UUID,
        session_id_str: str,
        request: RecordUpdateRequest,
        doctor_id: str | None,
        trace_id: str,
        x_state_version: int | None,
    ) -> RecordUpdateResponse:
        """在持锁状态下执行编辑。"""
        # 1. 加载会话
        session = await self._load_session(sid, session_id_str)

        # 2. 校验阶段
        if session.current_stage not in _EDITABLE_STAGES:
            raise InvalidStageTransitionError(
                message="当前阶段不允许编辑病历",
                detail=(
                    f"session_id={session_id_str} current_stage={session.current_stage}，"
                    f"仅 {sorted(_EDITABLE_STAGES)} 阶段可编辑"
                ),
                retryable=False,
            )

        # 3. 校验 state_version
        if x_state_version is not None and x_state_version != session.state_version:
            raise InvalidStateVersionError(
                detail=(
                    f"session_id={session_id_str} 客户端版本 {x_state_version} "
                    f"!= 服务端版本 {session.state_version}"
                ),
                retryable=True,
            )

        # 4. 获取当前最新版本病历作为基准
        latest = await self._fetch_record(sid, None)  # None = latest
        if latest is None:
            raise RecordNotFoundError(
                detail=f"session_id={session_id_str} 无已有病历作为编辑基准",
                retryable=False,
            )

        new_version = latest.version + 1

        # 5. 计算新值（未提供的字段沿用旧值）
        new_text = (
            request.record_text
            if request.record_text is not None
            else latest.record_text
        )
        new_json = (
            request.record_json
            if request.record_json is not None
            else latest.record_json
        )

        # 6. 计算 diff
        diff = _compute_diff(
            latest.record_text, latest.record_json,
            request.record_text, request.record_json,
        )

        # 7. 写入新版本
        new_record = MedicalRecord(
            session_id=sid,
            version=new_version,
            record_text=new_text,
            record_json=new_json,
            diff_from_previous=diff if diff.get("changed_fields") else None,
            doctor_review_id=latest.doctor_review_id,
            disclaimer=latest.disclaimer,
            edited_by_doctor=True,
        )
        self._db.add(new_record)

        # 8. 更新 session state_version
        session.state_version += 1
        session.updated_at = _now()

        # 9. 写审计 record.edited（不记录完整病历全文）
        self._db.add(
            _audit_event(
                session_id=sid,
                event_type="record.edited",
                actor_type="doctor" if doctor_id else "system",
                actor_id=doctor_id,
                payload={
                    "record_id": str(new_record.id),
                    "version": new_version,
                    "previous_version": latest.version,
                    "diff": diff,
                    "state_version": session.state_version,
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
            )
        )

        await self._db.flush()
        await self._db.refresh(new_record)

        return RecordUpdateResponse(
            id=str(new_record.id),
            session_id=str(new_record.session_id),
            version=new_record.version,
            diff_from_previous=diff,
            edited_by_doctor=new_record.edited_by_doctor,
            doctor_review_id=(
                str(new_record.doctor_review_id) if new_record.doctor_review_id else None
            ),
            updated_at=new_record.updated_at,
        )

    # ------------------------------------------------------------------
    # export
    # ------------------------------------------------------------------

    async def export_record(
        self,
        session_id: str,
        *,
        format: str,
        version: str | None = None,
        trace_id: str,
    ) -> tuple[bytes, str, str]:
        """导出病历为指定格式。

        Args:
            session_id: 会话 ID。
            format: 导出格式（txt / json / md）。
            version: 版本号或 "latest"。
            trace_id: 追踪 ID。

        Returns:
            (content_bytes, content_type, filename) 三元组。

        Raises:
            SessionNotFoundError: 会话不存在。
            ExportFormatUnsupportedError: 不支持的格式。
            RecordNotFoundError: 病历不存在。
        """
        sid = self._parse_session_id(session_id)

        # 校验会话存在
        session = await self._ensure_session_exists(sid)

        # 校验格式
        fmt = format.lower()
        if fmt not in _SUPPORTED_EXPORT_FORMATS:
            raise ExportFormatUnsupportedError(
                message=f"不支持的导出格式: {format}，仅支持 txt / json / md",
                detail=f"session_id={session_id} format={format}",
                retryable=False,
            )

        # 解析 version
        version_int = self._resolve_version(version)

        # 查询病历
        record = await self._fetch_record(sid, version_int)
        if record is None:
            raise RecordNotFoundError(
                detail=(
                    f"session_id={session_id} version={version or 'latest'}"
                    " 无对应病历记录"
                ),
                retryable=False,
            )

        # 生成文件内容
        content, content_type = self._build_export_content(record, fmt)

        # 生成文件名
        filename = self._build_filename(session, record, fmt)

        # 写审计 record.exported（不记录完整导出内容）
        self._db.add(
            _audit_event(
                session_id=sid,
                event_type="record.exported",
                actor_type="doctor",
                actor_id=None,
                payload={
                    "format": fmt,
                    "version": record.version,
                    "record_id": str(record.id),
                    "trace_id": trace_id,
                },
                trace_id=trace_id,
            )
        )
        await self._db.flush()

        return content, content_type, filename

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _parse_session_id(self, session_id: str) -> uuid.UUID:
        """解析 session_id 字符串为 UUID。

        Raises:
            SessionNotFoundError: 格式非法。
        """
        try:
            return uuid.UUID(session_id)
        except ValueError as exc:
            raise SessionNotFoundError(
                detail=f"session_id={session_id} 格式非法",
                retryable=False,
            ) from exc

    async def _ensure_session_exists(self, sid: uuid.UUID) -> ConsultSession:
        """校验会话存在并返回。

        Raises:
            SessionNotFoundError: 会话不存在。
        """
        result = await self._db.execute(
            select(ConsultSession).where(ConsultSession.id == sid)
        )
        session = result.scalar_one_or_none()
        if session is None:
            raise SessionNotFoundError(
                detail=f"session_id={sid} 在数据库中未找到",
                retryable=False,
            )
        return session

    async def _load_session(
        self, sid: uuid.UUID, session_id_str: str
    ) -> ConsultSession:
        """加载会话（同 _ensure_session_exists，但提供更明确的错误信息）。"""
        return await self._ensure_session_exists(sid)

    def _resolve_version(self, version: str | None) -> int | None:
        """解析 version 参数：'latest' 或 None → None（查最新），
        整数 → int。

        Raises:
            XuanhuValidationError: 格式非法。
        """
        if version is None or version == "latest":
            return None
        try:
            v = int(version)
            if v < 1:
                raise ValueError("version must be >= 1")
            return v
        except (ValueError, TypeError) as exc:
            raise XuanhuValidationError(
                message=f"无效的 version 参数: {version}",
                detail=f"version 必须为 'latest' 或正整数，收到: {version}",
                retryable=False,
            ) from exc

    async def _fetch_record(
        self, sid: uuid.UUID, version: int | None
    ) -> MedicalRecord | None:
        """查询病历。

        version=None 时查最新版本；否则查指定版本。
        """
        if version is not None:
            stmt = (
                select(MedicalRecord)
                .where(
                    MedicalRecord.session_id == sid,
                    MedicalRecord.version == version,
                )
            )
        else:
            stmt = (
                select(MedicalRecord)
                .where(MedicalRecord.session_id == sid)
                .order_by(MedicalRecord.version.desc())
                .limit(1)
            )
        result = await self._db.execute(stmt)
        return result.scalar_one_or_none()

    def _build_export_content(
        self, record: MedicalRecord, fmt: str
    ) -> tuple[bytes, str]:
        """根据格式生成导出内容。

        Returns:
            (content_bytes, content_type)
        """
        if fmt == "txt":
            return record.record_text.encode("utf-8"), "text/plain; charset=utf-8"
        elif fmt == "json":
            export_obj: dict[str, Any] = {
                "record_id": str(record.id),
                "session_id": str(record.session_id),
                "version": record.version,
                "disclaimer": record.disclaimer,
                "edited_by_doctor": record.edited_by_doctor,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
                **record.record_json,
            }
            content = json.dumps(export_obj, ensure_ascii=False, indent=2)
            return content.encode("utf-8"), "application/json; charset=utf-8"
        elif fmt == "md":
            md_content = self._build_markdown(record)
            return md_content.encode("utf-8"), "text/markdown; charset=utf-8"
        else:
            raise ExportFormatUnsupportedError(
                message=f"不支持的导出格式: {fmt}",
                retryable=False,
            )

    def _build_markdown(self, record: MedicalRecord) -> str:
        """从病历 JSON 生成 Markdown。

        优先从 record_json 结构化字段生成，缺失时回退到 record_text。
        """
        rj = record.record_json or {}
        lines: list[str] = []

        # 标题
        lines.append("# 病历记录")
        lines.append("")

        # 基本信息
        patient_info = rj.get("patient_info", {})
        if patient_info:
            lines.append("## 基本信息")
            name = patient_info.get("name", "未记录")
            gender = patient_info.get("gender", "未记录")
            age = patient_info.get("age", "未记录")
            lines.append(f"- 姓名：{name}")
            lines.append(f"- 性别：{gender}")
            lines.append(f"- 年龄：{age}")
            lines.append("")

        # 主诉
        chief_complaint = rj.get("chief_complaint")
        if chief_complaint:
            lines.append("## 主诉")
            lines.append(str(chief_complaint))
            lines.append("")

        # 现病史
        present_illness = rj.get("present_illness")
        if present_illness:
            lines.append("## 现病史")
            lines.append(str(present_illness))
            lines.append("")

        # 既往史
        past_history = rj.get("past_history")
        if past_history:
            lines.append("## 既往史")
            lines.append(str(past_history))
            lines.append("")

        # 四诊摘要
        four_diagnosis = rj.get("four_diagnosis", {})
        if four_diagnosis:
            lines.append("## 四诊摘要")
            for key, label in [
                ("inspection", "望诊"),
                ("auscultation_olfaction", "闻诊"),
                ("inquiry", "问诊"),
                ("palpation", "切诊"),
            ]:
                val = four_diagnosis.get(key)
                if val:
                    lines.append(f"- {label}：{val}")
            lines.append("")

        # 辨证分析
        syndrome_analysis = rj.get("syndrome_analysis")
        if syndrome_analysis:
            lines.append("## 辨证分析")
            lines.append(str(syndrome_analysis))
            lines.append("")

        # 辨证结论
        syndrome = rj.get("syndrome")
        if syndrome:
            lines.append("## 辨证结论")
            lines.append(f"证型：{syndrome}")
            treatment = rj.get("treatment_principle")
            if treatment:
                lines.append(f"治法：{treatment}")
            lines.append("")

        # 处方
        formula = rj.get("formula")
        if formula:
            lines.append("## 处方")
            name = formula.get("name", "")
            if name:
                lines.append(f"方名：{name}")
            composition = formula.get("composition", [])
            if composition:
                herbs_str = "、".join(
                    f"{h.get('herb', '')}{h.get('dose', '')}{h.get('unit', 'g')}"
                    for h in composition
                    if isinstance(h, dict)
                )
                if herbs_str:
                    lines.append(f"组成：{herbs_str}")
            lines.append("")

        # 医嘱
        advice = rj.get("advice", [])
        if advice:
            lines.append("## 医嘱/调护")
            for a in advice:
                lines.append(f"- {a}")
            lines.append("")

        # 医师确认
        doctor_review = rj.get("doctor_review")
        if doctor_review:
            lines.append("## 医师确认")
            action = doctor_review.get("action", "未记录")
            reviewed_by = doctor_review.get("reviewed_by", "未记录")
            reviewed_at = doctor_review.get("reviewed_at", "未记录")
            lines.append(f"- 动作：{action}")
            lines.append(f"- 医师：{reviewed_by}")
            lines.append(f"- 时间：{reviewed_at}")
            lines.append("")

        # 免责声明
        lines.append("## 免责声明")
        lines.append(record.disclaimer)
        lines.append("")

        return "\n".join(lines)

    def _build_filename(
        self, session: ConsultSession, record: MedicalRecord, fmt: str
    ) -> str:
        """构造导出文件名，兼容中文与 ASCII fallback。

        规则：
        - 优先使用 patient_ref，否则使用 session_id 前8位
        - 格式：病历_{identifier}_{yyyyMMdd_HHmm}.{ext}
        """
        from datetime import datetime as dt

        ts = record.updated_at or record.created_at
        date_str = ts.strftime("%Y%m%d_%H%M") if ts is not None else dt.now().strftime("%Y%m%d_%H%M")

        identifier = session.patient_ref or session.id.hex[:8]
        ext_map = {"txt": "txt", "json": "json", "md": "md"}
        ext = ext_map.get(fmt, fmt)

        # ASCII fallback 文件名
        return f"medical_record_{identifier}_{date_str}.{ext}"
