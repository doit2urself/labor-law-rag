"""도메인 모델과 예외의 모양(불변성, 위치 인자, 분류 값, 상속 관계)을 검증한다."""

import dataclasses

import pytest
from pydantic import ValidationError

from app.domain.errors import (
    ExternalServiceError,
    LawApiAuthError,
    LawApiError,
    QuotaExceeded,
)
from app.domain.models import (
    Chunk,
    GeneratedAnswer,
    Hit,
    QuestionClass,
    SentenceOut,
    Violation,
)


def _chunk() -> Chunk:
    return Chunk(
        chunk_id="law:1:55:0:20260101",
        doc_type="law",
        title="근로기준법 제55조",
        text="사용자는 1주에 평균 1회 이상의 유급휴일을 보장하여야 한다.",
        law_name="근로기준법",
        article_no="55",
        paragraph=None,
        effective_date="2026-01-01",
        promulgation_date="2025-10-01",
        source_id="law:1",
        source_url=None,
        extra={},
    )


# 법령 인증 실패는 법령 API 오류의 한 종류여야 한다(호출하는 쪽이 한 번에 잡을 수 있게).
def test_law_api_auth_error_is_a_law_api_error():
    assert issubclass(LawApiAuthError, LawApiError)
    assert all(issubclass(e, Exception) for e in (QuotaExceeded, ExternalServiceError, LawApiError))


# 한도 초과 예외는 "몇 초 뒤에 다시 시도하라"는 안내(있을 때만)를 함께 나를 수 있어야 한다.
def test_quota_exceeded_carries_optional_retry_after():
    assert QuotaExceeded().retry_after_s is None
    assert QuotaExceeded("한도 초과", retry_after_s=12.5).retry_after_s == 12.5


# Chunk는 만든 뒤 바꿀 수 없어야 한다(색인·검색 사이에 몰래 바뀌지 않도록).
def test_chunk_and_hit_are_frozen():
    chunk = _chunk()
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunk.title = "바뀐 제목"  # type: ignore[misc]
    hit = Hit(chunk=chunk, score=0.9, rank=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.rank = 2  # type: ignore[misc]


# Violation은 위치 인자로 만들 수 있어야 한다(Task 13의 테스트가 이 형태로 만든다).
def test_violation_accepts_positional_args():
    v = Violation("unknown_citation_id", 0, "doc9가 없음")
    assert (v.code, v.sentence_index, v.detail) == ("unknown_citation_id", 0, "doc9가 없음")


# 질문 분류 값은 LLM 구조화 출력의 문자열(Analysis.qclass)과 같아야 한다.
def test_question_class_values_match_analysis_labels():
    assert [q.value for q in QuestionClass] == [
        "counsel",
        "verdict_request",
        "uncovered_labor",
        "out_of_scope",
    ]


# LLM 구조화 출력용 모델은 허용되지 않는 kind 값을 거부해야 한다.
def test_generated_answer_validates_sentence_kind():
    ok = GeneratedAnswer(
        sentences=[SentenceOut(text="a", citation_ids=["doc1"], kind="evidence")]
    )
    assert ok.sentences[0].citation_ids == ["doc1"]
    with pytest.raises(ValidationError):
        SentenceOut(text="a", citation_ids=[], kind="bogus")  # type: ignore[arg-type]
