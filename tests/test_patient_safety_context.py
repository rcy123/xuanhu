"""R4-A 患者安全上下文：确定性当前语义投影聚焦单测。

针对 ``app.services.langgraph_review._patient_info_from_domain`` 的核心承诺：
``patient.sex`` / ``patient.age`` 只从 ``project_current_observations`` 投影出的
**当前语义链头**读取——被 CORRECTED/RETRACTED 取代的根不是当前真值，输入顺序
也不决定赢家。同一组当前事实在任何历史顺序下投影出同一 PatientInfo；校正值胜出，
撤回值消失为 UNKNOWN/None；bool 年龄被拒绝，越界年龄安全忽略。函数不修改
observations / profile。
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations
from uuid import UUID

from app.schemas.domain import (
    ObservationSchema,
    ObservationStatus,
    SafetyProfileSchema,
)
from app.services.langgraph_review import (
    _observation_value,
    _parse_patient_age,
    _patient_info_from_domain,
)

BASE_TS = datetime(2026, 8, 10, 8, 0, 0, tzinfo=UTC)
SESSION = UUID("00000000-0000-0000-0000-000000000001")
SOURCE = UUID("00000000-0000-0000-0000-000000000099")


def _uuid(suffix: int) -> UUID:
    return UUID(f"00000000-0000-0000-0000-{suffix:012d}")


def _obs(
    *,
    key: str,
    value: object,
    obs_id: UUID,
    status: ObservationStatus = ObservationStatus.ACTIVE,
    target_id: UUID | None = None,
    normalized_value: object = None,
) -> ObservationSchema:
    return ObservationSchema(
        observation_id=obs_id,
        session_id=SESSION,
        fact_key=key,
        value=value,
        normalized_value=normalized_value,
        source_message_id=SOURCE,
        status=status,
        supersedes_observation_id=target_id,
        created_at=BASE_TS,
    )


def _profile() -> SafetyProfileSchema:
    return SafetyProfileSchema(session_id=SESSION)


def _sex(value: object, obs_id: int, **kw: object) -> ObservationSchema:
    return _obs(key="patient.sex", value=value, obs_id=_uuid(obs_id), **kw)


def _age(value: object, obs_id: int, **kw: object) -> ObservationSchema:
    return _obs(key="patient.age", value=value, obs_id=_uuid(obs_id), **kw)


class TestObservationValue:
    def test_prefers_normalized_value_over_value(self):
        fact = _obs(key="patient.age", value="0", normalized_value=30, obs_id=_uuid(10))
        assert _observation_value(fact) == 30

    def test_normalized_zero_is_not_falsy(self):
        fact = _obs(key="patient.age", value="0", normalized_value=0, obs_id=_uuid(10))
        assert _observation_value(fact) == 0

    def test_falls_back_to_value_when_normalized_is_none(self):
        fact = _obs(key="patient.sex", value="male", normalized_value=None, obs_id=_uuid(10))
        assert _observation_value(fact) == "male"


class TestParsePatientAge:
    def test_bool_rejected(self):
        assert _parse_patient_age(True) is None
        assert _parse_patient_age(False) is None

    def test_none_rejected(self):
        assert _parse_patient_age(None) is None

    def test_non_int_non_str_rejected(self):
        assert _parse_patient_age(45.0) is None
        assert _parse_patient_age([45]) is None

    def test_int_accepted_including_zero(self):
        assert _parse_patient_age(0) == 0
        assert _parse_patient_age(45) == 45
        assert _parse_patient_age(130) == 130

    def test_int_parseable_string_accepted(self):
        assert _parse_patient_age("45") == 45
        assert _parse_patient_age(" 45 ") == 45

    def test_blank_or_invalid_string_rejected(self):
        assert _parse_patient_age("") is None
        assert _parse_patient_age("  ") is None
        assert _parse_patient_age("abc") is None

    def test_out_of_range_ignored(self):
        assert _parse_patient_age(-1) is None
        assert _parse_patient_age(131) is None
        assert _parse_patient_age("200") is None


class TestPatientInfoFromDomain:
    def test_active_sex_and_age(self):
        obs = (_sex("male", 10), _age("30", 20))
        info = _patient_info_from_domain(_profile(), obs)
        assert info.gender == "male"
        assert info.age == 30

    def test_corrected_sex_wins(self):
        root = _sex("male", 10)
        corrected = _sex("female", 11, status=ObservationStatus.CORRECTED, target_id=_uuid(10))
        # 无论历史顺序如何，校正值胜出（输入顺序不决定赢家）。
        for obs in ((root, corrected), (corrected, root)):
            info = _patient_info_from_domain(_profile(), obs)
            assert info.gender == "female"

    def test_retracted_sex_becomes_unknown(self):
        root = _sex("male", 10)
        tombstone = _sex(None, 12, status=ObservationStatus.RETRACTED, target_id=_uuid(10))
        info = _patient_info_from_domain(_profile(), (root, tombstone))
        assert info.gender == "unknown"

    def test_corrected_age_wins(self):
        root = _age("30", 10)
        corrected = _age(35, 11, status=ObservationStatus.CORRECTED, target_id=_uuid(10))
        info = _patient_info_from_domain(_profile(), (root, corrected))
        assert info.age == 35

    def test_retracted_age_becomes_none(self):
        root = _age(30, 10)
        tombstone = _age(None, 12, status=ObservationStatus.RETRACTED, target_id=_uuid(10))
        info = _patient_info_from_domain(_profile(), (root, tombstone))
        assert info.age is None

    def test_two_active_roots_deterministic_by_observation_id(self):
        # 两条同为当前事实的 patient.sex 根：投影按 observation_id 稳定排序，
        # 后序 id 确定性胜出，与输入顺序无关。
        low = _sex("male", 10)
        high = _sex("female", 11)
        assert _patient_info_from_domain(_profile(), (low, high)).gender == "female"
        assert _patient_info_from_domain(_profile(), (high, low)).gender == "female"

    def test_shuffled_history_determinism(self):
        obs = (
            _sex("male", 10),
            _sex("female", 11, status=ObservationStatus.CORRECTED, target_id=_uuid(10)),
            _age("30", 20),
            _age(35, 21, status=ObservationStatus.CORRECTED, target_id=_uuid(20)),
        )
        expected = None
        for perm in permutations(obs):
            info = _patient_info_from_domain(_profile(), perm)
            if expected is None:
                expected = info
            else:
                assert info.gender == expected.gender
                assert info.age == expected.age
        assert expected.gender == "female"
        assert expected.age == 35

    def test_bool_age_rejected(self):
        info = _patient_info_from_domain(_profile(), (_age(True, 10),))
        assert info.age is None

    def test_bool_corrected_age_yields_none(self):
        # 校正值是当前链头；bool 年龄被拒绝后不留残余真值（不回溯被取代的根）。
        root = _age(30, 10)
        bogus = _age(True, 11, status=ObservationStatus.CORRECTED, target_id=_uuid(10))
        info = _patient_info_from_domain(_profile(), (root, bogus))
        assert info.age is None

    def test_out_of_range_age_ignored(self):
        info = _patient_info_from_domain(_profile(), (_age(200, 10), _age(-1, 11)))
        assert info.age is None

    def test_age_zero_accepted(self):
        info = _patient_info_from_domain(_profile(), (_age(0, 10),))
        assert info.age == 0
        info2 = _patient_info_from_domain(_profile(), (_age("0", 10),))
        assert info2.age == 0

    def test_normalized_age_preferred(self):
        obs = (_obs(key="patient.age", value="0", normalized_value=45, obs_id=_uuid(10)),)
        info = _patient_info_from_domain(_profile(), obs)
        assert info.age == 45

    def test_invalid_sex_ignored(self):
        info = _patient_info_from_domain(_profile(), (_sex("nonbinary", 10),))
        assert info.gender == "unknown"

    def test_non_string_sex_ignored(self):
        info = _patient_info_from_domain(_profile(), (_sex(123, 10),))
        assert info.gender == "unknown"

    def test_sex_case_and_whitespace_normalized(self):
        info = _patient_info_from_domain(_profile(), (_sex("  MALE  ", 10),))
        assert info.gender == "male"
        info2 = _patient_info_from_domain(_profile(), (_sex("Female", 10),))
        assert info2.gender == "female"

    def test_does_not_mutate_inputs(self):
        obs = (
            _sex("male", 10),
            _sex("female", 11, status=ObservationStatus.CORRECTED, target_id=_uuid(10)),
            _age("30", 20),
            _age(35, 21, status=ObservationStatus.CORRECTED, target_id=_uuid(20)),
        )
        obs_before = tuple(o.model_dump(mode="json") for o in obs)
        profile = _profile()
        profile_before = profile.model_dump(mode="json")
        _patient_info_from_domain(profile, obs)
        assert tuple(o.model_dump(mode="json") for o in obs) == obs_before
        assert profile.model_dump(mode="json") == profile_before
