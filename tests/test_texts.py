"""화면·답변에 반드시 들어가야 하는 고정 문구와, 가짜 구현체가 포트를 만족하는지 검증한다."""

from app.ports import EmbedderPort, IndexPort, LlmPort, RerankerPort, SearchPort
from app.texts import (
    DISCLAIMER,
    PII_NOTICE,
    QUOTA_MESSAGE,
    SOURCE_NOTICE,
    limit_exceeded_message,
)
from tests.conftest import FakeEmbedder, FakeIndex, FakeLlm, FakeReranker, FakeSearch


# 고정 문구가 한 글자도 다르지 않은지 확인한다(법제처 출처 표기는 법적 요건이라 정확해야 한다).
def test_fixed_copy_is_exact():
    assert limit_exceeded_message(5) == "이 IP의 사용 횟수(5회)를 초과했습니다."
    assert DISCLAIMER == "법률 자문이 아닙니다. 출처의 원문을 확인하세요"
    assert SOURCE_NOTICE == "출처: 법제처 국가법령정보센터"
    assert PII_NOTICE == "개인정보를 입력하지 마세요"
    assert QUOTA_MESSAGE == "오늘의 무료 사용 한도가 소진되었습니다. 나중에 다시 시도해 주세요"


# 가짜 구현체들이 포트(인터페이스)의 메서드를 모두 갖췄는지 확인한다.
def test_fakes_satisfy_ports():
    assert isinstance(FakeEmbedder(), EmbedderPort) and isinstance(FakeSearch([]), SearchPort)
    assert isinstance(FakeLlm(), LlmPort) and isinstance(FakeIndex(), IndexPort)
    assert isinstance(FakeReranker(), RerankerPort)
