"""Cohere Rerank 어댑터: 검색 후보 20개를 질문과의 관련성으로 다시 줄 세운다 (RerankerPort 구현).

역할: 검색(BM25+벡터+RRF)이 준 후보를 "이 질문에 정말 관련 있는 순서"로 다시 정렬해 상위 5개를 고른다.
사용처: app/retrieval/rerank_gate.py(월 호출 한도 게이트가 이 어댑터를 감싼다).
입력: 질문, 후보(Hit) 목록, top_n. 출력: 점수(relevance_score)가 채워진 상위 Hit 목록, 순위는 1부터.

오류 변환: 어댑터는 Cohere 라이브러리의 예외를 우리 예외로 바꿔 던진다(app/domain/errors.py의 설명 참고).
- 429(한도 초과) → QuotaExceeded, 그 밖의 Cohere·네트워크 오류 → ExternalServiceError.
그러면 게이트와 그래프는 Cohere를 몰라도 "한도인지, 그 밖의 실패인지"만 구분하면 된다.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import cohere
import httpx
from cohere.core.api_error import ApiError

from app.domain.errors import ExternalServiceError, QuotaExceeded
from app.domain.models import Hit

# Cohere 공식 문서(docs.cohere.com/docs/rerank-overview)의 모델 목록에 있는 Rerank 5 이름. 2026-11-18 확인.
DEFAULT_RERANK_MODEL = "rerank-v5.0-pro"


class CohereReranker:
    """Cohere Rerank API를 부르는 리랭커.

    client: 테스트에서 가짜 클라이언트를 넣는 자리. 없으면 진짜 `cohere.ClientV2`를 만든다.
    (진짜 클라이언트를 만들 때는 네트워크를 쓰지 않는다. 호출할 때만 쓴다.)
    """

    def __init__(self, api_key: str, model: str = DEFAULT_RERANK_MODEL, client: Any = None) -> None:
        self._model = model
        self._client = client if client is not None else cohere.ClientV2(api_key=api_key)

    def rerank(self, query: str, hits: list[Hit], top_n: int) -> list[Hit]:
        """후보 중 질문과 관련성이 높은 `top_n`개를 점수 높은 순으로 돌려준다.

        돌려주는 Hit는 원래 후보의 청크를 그대로 두고 `score`를 리랭크 점수로, `rank`를 1부터 다시 매긴 것이다.
        """
        if not hits:
            return []  # 후보가 없으면 호출하지 않는다 — 월 호출 한도를 아낀다.
        top_n = min(top_n, len(hits))
        try:
            response = self._client.rerank(
                model=self._model,
                query=query,
                documents=[hit.chunk.text for hit in hits],
                top_n=top_n,
            )
        except cohere.TooManyRequestsError as exc:  # ApiError의 하위 클래스라서 반드시 먼저 잡는다.
            raise QuotaExceeded(str(exc)) from exc
        except (ApiError, httpx.HTTPError) as exc:
            raise ExternalServiceError(f"Cohere 리랭크 호출 실패: {type(exc).__name__}") from exc

        # 응답의 각 결과는 `index`(우리가 보낸 documents 안의 위치)와 `relevance_score`를 가진다.
        # 응답이 점수 순으로 정렬돼 있다는 보장은 문서로 확인하지 못했으므로 우리가 직접 정렬한다.
        results = list(response.results)
        for result in results:
            if not 0 <= result.index < len(hits):
                raise ExternalServiceError(f"Cohere 응답의 index({result.index})가 후보 범위 밖입니다")
        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return [
            dataclasses.replace(hits[r.index], score=r.relevance_score, rank=position)
            for position, r in enumerate(results[:top_n], start=1)
        ]
