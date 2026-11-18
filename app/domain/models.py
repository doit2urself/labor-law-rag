"""서비스 전체가 주고받는 데이터 모델.

역할: 검색 단위(Chunk), 검색 결과(Hit), 질문 분류(QuestionClass), LLM이 만드는 답변·검증 결과 등을
한곳에 정의한다. 계획서의 모든 Task가 여기 있는 이름을 그대로 가져다 쓴다.

모양을 둘로 나눈 이유:
- dataclass(frozen=True): 만든 뒤 바뀌면 안 되는 값(Chunk, Hit 등). 색인·검색 도중에 몰래 바뀌는 사고를 막는다.
- pydantic BaseModel: LLM의 구조화 출력으로 받는 값(GeneratedAnswer, Verification 등).
  LLM이 규칙에 어긋난 JSON을 주면 만드는 순간 검증 오류로 걸러낸다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Literal

from pydantic import BaseModel

# 평가용 정답 키: (법령명, 조문번호). 청크 ID 대신 쓰는 이유는 청크를 다시 잘라도 정답이 유효하기 때문이다(스펙 6.8).
GoldKey = tuple[str, str]


@dataclass(frozen=True)
class Chunk:
    """검색의 최소 단위(법령 한 조문, 또는 고용노동부 해석 한 건).

    chunk_id: 청크 고유 ID. 같은 입력이면 항상 같은 값이어야 다시 색인해도 덮어쓰기가 된다.
    source_id: 청크가 나온 원문(법령 또는 해석)의 ID. 원문이 사라지면 그 청크를 한꺼번에 지우는 데 쓴다.
    extra: 위 칸에 들어가지 않는 메타데이터(안건번호, 해석일자, 임베딩 모델명·차원 등).
    """

    chunk_id: str
    doc_type: Literal["law", "interpretation"]
    title: str
    text: str
    law_name: str | None
    article_no: str | None
    paragraph: str | None
    effective_date: str | None
    promulgation_date: str | None
    source_id: str
    source_url: str | None
    extra: dict


@dataclass(frozen=True)
class Hit:
    """검색 결과 한 건: 어떤 청크가, 어떤 점수로, 몇 등으로 나왔는가."""

    chunk: Chunk
    score: float
    rank: int


class QuestionClass(StrEnum):
    """질문을 네 가지로 나눈 분류. 값은 LLM 구조화 출력의 문자열과 같다(auto = 이름의 소문자).

    COUNSEL: 규칙을 설명해 달라는 일반 상담 / VERDICT_REQUEST: 내 사건의 결론·승패를 요구
    UNCOVERED_LABOR: 근로 분야지만 수집한 법령 밖 / OUT_OF_SCOPE: 근로 분야가 아님
    """

    COUNSEL = auto()
    VERDICT_REQUEST = auto()
    UNCOVERED_LABOR = auto()
    OUT_OF_SCOPE = auto()


class SentenceOut(BaseModel):
    """답변의 한 문장. 문장마다 어느 문서를 근거로 했는지(citation_ids)를 함께 낸다.

    kind: "evidence"는 문서를 근거로 한 문장(인용 필수), "general"은 인사·안내처럼 근거가 필요 없는 문장.
    """

    text: str
    citation_ids: list[str]
    kind: Literal["evidence", "general"]


class GeneratedAnswer(BaseModel):
    """LLM이 만든 답변 전체(문장 목록)."""

    sentences: list[SentenceOut]


class Verification(BaseModel):
    """LLM 근거 검증의 결과.

    sentences_ok: 문장별로 인용 문서가 실제로 뒷받침하는지.
    verdict_found: 사용자의 사건에 대한 결론·판정 문장이 섞여 있는지(있으면 안 된다).
    """

    sentences_ok: list[bool]
    verdict_found: bool
    notes: str


@dataclass(frozen=True)
class Violation:
    """코드 검사가 찾아낸 규칙 위반 한 건. 예: 존재하지 않는 인용 ID, 근거 문장인데 인용이 없음."""

    code: str
    sentence_index: int | None
    detail: str


@dataclass(frozen=True)
class RerankResult:
    """리랭크 단계의 결과.

    used_reranker: 실제로 리랭커를 썼는가. 월 호출 한도(cap)나 오류(error)면 RRF 상위 결과를 그대로 쓰고 False.
    """

    hits: list[Hit]
    used_reranker: bool
    reason: Literal["ok", "cap", "error"]


@dataclass(frozen=True)
class IndexReport:
    """색인 작업 보고: 색인한 수, 평가용 홀드아웃이라 건너뛴 수, 원문이 사라져 지운 수."""

    indexed: int
    skipped_holdout: int
    deleted: int
