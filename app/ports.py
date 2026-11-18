"""포트(인터페이스) 모음: 외부 서비스와 만나는 부분의 "약속".

개념(포트와 어댑터): 그래프·API 로직은 Gemini나 OpenSearch를 직접 부르지 않고 여기 정의한 약속만 안다.
진짜 구현(어댑터)은 adapters/에 있고, 테스트에서는 tests/conftest.py의 가짜 구현으로 바꿔 끼운다.
덕분에 (1) 외부 서비스 없이 빠르게 시험할 수 있고 (2) 공급자를 바꿔도 로직은 그대로다
(예: Gemini 무료 한도가 모자라면 다른 모델로 교체).

`typing.Protocol`은 "이 메서드들을 가졌으면 이 약속을 지킨 것"이라는 구조적 타입이다.
`@runtime_checkable`을 달면 `isinstance(객체, 포트)`로 메서드 유무를 검사할 수 있다.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from app.domain.models import Chunk, Hit, RerankResult


@runtime_checkable
class EmbedderPort(Protocol):
    """글을 숫자 벡터로 바꾸는 임베딩 모델."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """색인할 문서들을 벡터로 바꾼다(입력 순서와 같은 순서로 반환)."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """검색 질문 하나를 벡터로 바꾼다."""
        ...


@runtime_checkable
class LlmPort(Protocol):
    """구조화된 JSON 답을 돌려주는 LLM."""

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        tags: list[str] | None = None,
    ) -> BaseModel:
        """system/user 프롬프트를 보내고 `schema` 모양으로 검증된 결과를 받는다.

        tags: 호출에 붙이는 꼬리표. 예: "nostream"은 이 호출의 출력이 클라이언트로 흘러가지 않게 한다.
        """
        ...


@runtime_checkable
class SearchPort(Protocol):
    """질문으로 후보 문서를 찾는 검색기(하이브리드 검색)."""

    def search(
        self,
        text: str,
        vector: list[float],
        *,
        size: int = 20,
        only_current: bool = True,
    ) -> list[Hit]:
        """글(text)과 벡터(vector)로 상위 `size`개 후보를 찾는다. only_current면 현행 법령만 대상으로 한다."""
        ...


@runtime_checkable
class IndexPort(Protocol):
    """문서를 검색엔진에 넣고 빼는 색인 관리."""

    def ensure_index(self, dim: int) -> None:
        """색인이 없으면 `dim`차원 벡터용으로 만든다."""
        ...

    def bulk_upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """청크와 벡터를 묶음으로 넣는다(같은 chunk_id는 덮어쓴다). 넣은 개수를 반환."""
        ...

    def delete_by_source(self, source_ids: set[str]) -> int:
        """주어진 원문(source_id)에서 나온 청크를 모두 지운다. 지운 개수를 반환."""
        ...

    def list_source_ids(self) -> set[str]:
        """지금 색인에 들어 있는 원문 ID 전체."""
        ...


@runtime_checkable
class RerankerPort(Protocol):
    """후보를 질문과의 관련성으로 다시 줄 세우는 리랭커(실제 외부 호출)."""

    def rerank(self, query: str, hits: list[Hit], top_n: int) -> list[Hit]:
        """후보 중 관련성 높은 `top_n`개를 순서대로 반환한다."""
        ...


@runtime_checkable
class RerankStage(Protocol):
    """그래프가 쓰는 리랭크 단계. 한도·오류 때는 리랭커 대신 기존 순위를 쓰는 폴백까지 책임진다."""

    def rerank(self, query: str, hits: list[Hit], top_n: int = 5) -> RerankResult:
        """리랭크를 시도하고, 쓰지 못했으면 그 이유(reason)와 함께 기존 상위 결과를 돌려준다."""
        ...


@runtime_checkable
class EventSink(Protocol):
    """그래프가 남기는 수동 기록 이벤트(질문 분류, 리랭크 폴백 여부, 검증 결과, 갈림길 결과 등)를 받는 곳.

    운영에서는 Langfuse에 이벤트로 남기고, 테스트에서는 목록에 적어 둔다. 그래프는 이것이 무엇인지 모른다.
    넘겨받는 `payload`는 그래프가 이미 마스킹을 거친 값이다(개인정보는 기록에 남지 않는다).
    """

    def __call__(self, name: str, payload: dict) -> None:
        """이벤트 하나를 기록한다. 실패해도 그래프는 멈추지 않는다(호출한 쪽이 예외를 삼킨다)."""
        ...
