"""수집 대상 법령 목록(COVERED_LAWS)의 기본 성질을 검증한다."""

from app.scope import COVERED_LAWS


# 스펙 6.2가 정한 핵심 법령 6종이 모두 들어 있고, 이름이 중복되지 않는지 확인한다.
def test_covered_laws_contain_core_laws_without_duplicates():
    core = {
        "근로기준법",
        "최저임금법",
        "근로자퇴직급여 보장법",
        "기간제 및 단시간근로자 보호 등에 관한 법률",
        "남녀고용평등과 일ㆍ가정 양립 지원에 관한 법률",  # `ㆍ`(U+318D): 법제처 파일의 실제 법령명
        "임금채권보장법",
    }
    assert core <= set(COVERED_LAWS)
    assert len(COVERED_LAWS) == len(set(COVERED_LAWS))
