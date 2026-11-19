"""OpenSearch 어댑터: 색인 매핑, RRF 파이프라인, 하이브리드 검색 쿼리, 그리고 이를 감싼 `OpenSearchStore`.

역할: 법령 조문 청크를 OpenSearch에 넣고(IndexPort), 질문으로 후보를 찾는다(SearchPort).
사용처: Task 7(색인 파이프라인)이 IndexPort로, Task 10~13의 검색 노드가 SearchPort로 쓴다.
입출력: 쿼리를 만드는 함수 셋(`build_*`)은 서버 없이 시험할 수 있는 순수 함수(딕셔너리 → 딕셔너리)이고,
`OpenSearchStore`만 실제 클라이언트(opensearch-py)를 부른다.

핵심 개념 (모두 서버가 아니라 이 파일의 쿼리로 처리한다 — 스펙 접근 방식 A):
- Nori: 한국어 형태소 분석기. "주휴수당을"에서 조사를 떼어 "주휴수당"으로 찾게 해 준다.
  색인할 때와 검색할 때 같은 분석기를 써야 같은 단어로 갈라져서 맞는다.
- 하이브리드 검색: 글자 검색(BM25) · 조문번호 정확 일치 · 벡터(의미) 검색을 한 번에 던진다.
- RRF(Reciprocal Rank Fusion): 서로 점수 단위가 다른 검색 결과들을 "점수"가 아니라 "순위"로 합친다.
  각 목록에서 r등이면 1/(rank_constant + r)점을 주고 더한다. 단위를 맞출 필요가 없어서 단순하고 튼튼하다.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

from opensearchpy import OpenSearch

from app.domain.errors import ExternalServiceError
from app.domain.models import Chunk, Hit
from app.scope import COVERED_LAWS

# 공식 문서(docs.opensearch.org)의 HNSW 예시 값. m=16은 스펙의 메모리 계산(청크 10만 개 0.352GB)의 전제다.
_HNSW_M = 16
_HNSW_EF_CONSTRUCTION = 100

# 상태 확인(`OpenSearchStore.health`)이 응답을 기다리는 최대 시간(초). 이 호출은 로그인 없이 부를 수 있어서 짧게 끊는다.
_HEALTH_TIMEOUT_S = 3

# "제55조", "제 55 조", "제43조의2"에서 조문번호를 뽑는 규칙. 가지번호는 "43의2" 꼴로 모은다
# (파서의 Chunk.article_no와 같은 표기라서 keyword 정확 일치가 된다).
_ARTICLE_RE = re.compile(r"제\s*(\d+)\s*조(?:\s*의\s*(\d+))?")


def build_client_kwargs(url: str, user: str, password: str, *, verify_certs: bool = False) -> dict:
    """`opensearchpy.OpenSearch(...)`에 넘길 접속 인자를 만든다(접속은 하지 않는다).

    verify_certs 기본값이 False인 이유: 설치 직후 OpenSearch의 HTTPS 인증서는 자체 서명이라 검증하면 실패한다.
    이 연결은 같은 서버 안(127.0.0.1)에서만 오가므로 중간에서 가로챌 길이 없다. 서버 밖 주소로 바꿀 때는
    반드시 True(+CA 인증서)로 바꿔야 한다. 검증을 끈 상태의 경고는 요청마다 쏟아지므로 함께 끈다.
    """
    parsed = urlparse(url)
    return {
        "hosts": [{"host": parsed.hostname, "port": parsed.port or 9200}],
        "http_auth": (user, password),
        "use_ssl": parsed.scheme == "https",
        "verify_certs": verify_certs,
        "ssl_show_warn": verify_certs,
    }


def make_client(url: str, user: str, password: str, *, verify_certs: bool = False) -> OpenSearch:
    """접속 인자(`build_client_kwargs`)로 OpenSearch 클라이언트를 만든다. 만들 때 네트워크를 쓰지 않는다."""
    return OpenSearch(**build_client_kwargs(url, user, password, verify_certs=verify_certs))


def build_index_body(dim: int, *, engine: str = "faiss", space_type: str = "cosinesimil") -> dict:
    """색인 생성 요청 본문(설정 + 매핑)을 만든다.

    - `text`·`title`: Nori로 분석한다. `analyzer`만 주면 검색 때도 같은 분석기가 쓰인다(search_analyzer 생략).
    - `law_name`·`article_no`·`source_id`·`doc_type`: keyword. 쪼개지 않고 통째로 일치해야 "43"과 "43의2"가 안 섞인다.
    - `embedding`: knn_vector(HNSW, m=16). `embedding_model`·`embedding_dim`을 함께 저장해 두는 이유는
      모델을 바꾸면 전체를 다시 색인해야 하는데, 어느 모델로 만든 색인인지 나중에 알아야 하기 때문이다.
    - 단일 노드 서버이므로 샤드 1, 복제본 0. 복제본이 1이면 놓을 곳이 없어 색인 상태가 늘 yellow가 된다.

    engine·space_type은 서버에서 확인할 값이다(docs/…/opensearch-notes.md): faiss HNSW가 cosinesimil을
    받아들이는지 공식 문서에서 확인하지 못했다. 거부되면 engine="lucene"으로 바꾼다.
    """
    return {
        "settings": {"index": {"knn": True, "number_of_shards": 1, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "chunk_id": {"type": "keyword"},
                "doc_type": {"type": "keyword"},
                "title": {"type": "text", "analyzer": "nori"},
                "text": {"type": "text", "analyzer": "nori"},
                "law_name": {"type": "keyword"},
                "article_no": {"type": "keyword"},
                "paragraph": {"type": "keyword"},
                "effective_date": {"type": "date", "format": "yyyy-MM-dd"},
                "promulgation_date": {"type": "date", "format": "yyyy-MM-dd"},
                "source_id": {"type": "keyword"},
                "source_url": {"type": "keyword", "index": False},
                "embedding_model": {"type": "keyword"},
                "embedding_dim": {"type": "integer"},
                # extra는 되돌려 읽기만 하고 검색하지 않으므로 색인하지 않는다(enabled: False).
                "extra": {"type": "object", "enabled": False},
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dim,
                    "space_type": space_type,
                    "method": {
                        "name": "hnsw",
                        "engine": engine,
                        "parameters": {"m": _HNSW_M, "ef_construction": _HNSW_EF_CONSTRUCTION},
                    },
                },
            }
        },
    }


def build_rrf_pipeline_body(rank_constant: int = 60) -> dict:
    """RRF로 하위 쿼리 결과를 합치는 검색 파이프라인(score-ranker-processor, OpenSearch 2.19+) 본문.

    rank_constant는 순위 차이를 얼마나 완만하게 볼지 정하는 값이다(기본 60, 허용 범위 1~10000).
    클수록 1등과 10등의 점수 차이가 작아진다.
    """
    return {
        "description": "하위 쿼리(BM25·정확 일치·벡터)의 순위를 RRF로 합친다",
        "phase_results_processors": [
            {"score-ranker-processor": {"combination": {"technique": "rrf", "rank_constant": rank_constant}}}
        ],
    }


def extract_exact_terms(text: str) -> tuple[list[str], list[str]]:
    """질문에서 (조문번호 목록, 법령명 목록)을 뽑는다. 정확 일치 하위 쿼리의 재료다.

    법령명은 수집 범위(COVERED_LAWS)에 있는 이름이 글에 그대로 들어 있을 때만 잡는다.
    "근로기준법 시행령"처럼 긴 이름 안에 "근로기준법"이 들어 있으므로, 긴 이름부터 찾고 이미 잡힌 자리와
    겹치는 짧은 이름은 버린다. 같은 값은 한 번만, 글에 나온 순서대로 돌려준다.
    """
    articles: list[str] = []
    for m in _ARTICLE_RE.finditer(text):
        number = f"{m.group(1)}의{m.group(2)}" if m.group(2) else m.group(1)
        if number not in articles:
            articles.append(number)

    taken: list[tuple[int, int, str]] = []  # (시작, 끝, 법령명)
    for name in sorted(COVERED_LAWS, key=len, reverse=True):
        for m in re.finditer(re.escape(name), text):
            if not any(m.start() < end and start < m.end() for start, end, _ in taken):
                taken.append((m.start(), m.end(), name))
    laws: list[str] = []
    for _, _, name in sorted(taken):
        if name not in laws:
            laws.append(name)
    return articles, laws


def _current_filter(today: str) -> dict:
    """"지금 유효한 문서만" 거르는 조건. 시행일이 없는 문서는 통과시키고, 있으면 오늘까지 시행된 것만 통과시킨다.

    필터는 쿼리 바깥(검색 뒤)이 아니라 hybrid 안에 둔다. 검색 뒤에 거르면 20개를 뽑고 나서 일부가 빠져
    20개보다 적게 남을 수 있다.
    """
    return {
        "bool": {
            "should": [
                {"bool": {"must_not": [{"exists": {"field": "effective_date"}}]}},
                {"range": {"effective_date": {"lte": today}}},
            ],
            "minimum_should_match": 1,
        }
    }


def build_hybrid_query(
    text: str,
    vector: list[float],
    *,
    depth: int = 150,
    size: int = 20,
    only_current: bool = True,
    today: str | None = None,
) -> dict:
    """하이브리드 검색 요청 본문을 만든다. RRF 파이프라인과 함께 쓴다.

    하위 쿼리(공식 문서 기준 최대 5개):
    1. Nori `match`: 본문에서 단어로 찾는다. `match`는 질문을 문법 없는 일반 글로 다루므로 `"`·`*`·`\\`가
       들어 있어도 쿼리 문법으로 해석되지 않는다(그래서 query_string을 쓰지 않는다).
    2. 정확 일치(조문번호가 있을 때만): 조문번호는 반드시 일치(must), 법령명은 일치하면 점수를 더 준다(should).
       조문번호가 없는데 만들면 빈 조건이 모든 문서와 맞아 버리므로 아예 만들지 않는다.
    3. knn: 벡터가 가까운 문서를 depth개 가져온다.
    각 하위 쿼리에서 depth개씩 가져오게(`pagination_depth`) 한 뒤 RRF로 합쳐 상위 `size`개를 돌려준다.
    응답에서 벡터(`embedding`)는 빼서 전송량을 줄인다.
    """
    queries: list[dict[str, Any]] = [{"match": {"text": text}}]

    articles, laws = extract_exact_terms(text)
    if articles:
        exact: dict[str, Any] = {"must": [{"terms": {"article_no": articles}}]}
        if laws:
            exact["should"] = [{"terms": {"law_name": laws}}]
        queries.append({"bool": exact})

    queries.append({"knn": {"embedding": {"vector": vector, "k": depth}}})

    hybrid: dict[str, Any] = {"queries": queries, "pagination_depth": depth}
    if only_current:
        hybrid["filter"] = _current_filter(today or date.today().isoformat())

    return {"query": {"hybrid": hybrid}, "size": size, "_source": {"excludes": ["embedding"]}}


def chunk_to_doc(chunk: Chunk, vector: list[float]) -> dict:
    """청크와 벡터를 색인 문서(딕셔너리)로 바꾼다. 문서 ID는 호출하는 쪽이 chunk_id로 정한다."""
    return {
        "chunk_id": chunk.chunk_id,
        "doc_type": chunk.doc_type,
        "title": chunk.title,
        "text": chunk.text,
        "law_name": chunk.law_name,
        "article_no": chunk.article_no,
        "paragraph": chunk.paragraph,
        "effective_date": chunk.effective_date,
        "promulgation_date": chunk.promulgation_date,
        "source_id": chunk.source_id,
        "source_url": chunk.source_url,
        "extra": dict(chunk.extra),
        "embedding_model": chunk.extra.get("embedding_model"),
        "embedding_dim": len(vector),
        "embedding": vector,
    }


def doc_to_chunk(doc: dict) -> Chunk:
    """검색 결과의 `_source`를 청크로 되돌린다. 벡터와 임베딩 메타 칸은 Chunk에 없으므로 읽지 않는다."""
    return Chunk(
        chunk_id=doc["chunk_id"],
        doc_type=doc["doc_type"],
        title=doc["title"],
        text=doc["text"],
        law_name=doc.get("law_name"),
        article_no=doc.get("article_no"),
        paragraph=doc.get("paragraph"),
        effective_date=doc.get("effective_date"),
        promulgation_date=doc.get("promulgation_date"),
        source_id=doc["source_id"],
        source_url=doc.get("source_url"),
        extra=dict(doc.get("extra") or {}),
    )


class OpenSearchStore:
    """opensearch-py 클라이언트를 감싸 `IndexPort`와 `SearchPort`를 구현한다.

    client: `opensearchpy.OpenSearch` (테스트에서는 같은 메서드를 가진 가짜).
    depth: 하위 쿼리마다 가져올 후보 수. 포트의 `search()`에는 depth가 없어서 생성자로 받는다
    (설정의 `search_depth`를 Task 14의 조립 코드가 넘긴다).
    """

    def __init__(self, client: Any, index: str, pipeline: str = "rrf-pipeline", *, depth: int = 150) -> None:
        self._client = client
        self._index = index
        self._pipeline = pipeline
        self._depth = depth

    def ensure_index(self, dim: int) -> None:
        """색인이 없으면 만들고, RRF 파이프라인을 등록한다.

        색인은 이미 있으면 건드리지 않는다(데이터 보존). 파이프라인 등록은 같은 이름이면 덮어쓰므로 매번 해도 안전하다.
        """
        if not self._client.indices.exists(index=self._index):
            self._client.indices.create(index=self._index, body=build_index_body(dim))
        self._client.search_pipeline.put(id=self._pipeline, body=build_rrf_pipeline_body())

    def bulk_upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """청크를 묶음으로 넣는다. 문서 ID가 chunk_id라서 같은 청크는 덮어쓴다(다시 색인해도 중복이 안 생긴다).

        일부라도 거부되면(`errors: true`) 성공으로 보고하지 않고 예외를 낸다.
        """
        if len(chunks) != len(vectors):
            raise ValueError(f"청크 {len(chunks)}개와 벡터 {len(vectors)}개의 수가 다릅니다")
        if not chunks:
            return 0
        body: list[dict] = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            body.append({"index": {"_index": self._index, "_id": chunk.chunk_id}})
            body.append(chunk_to_doc(chunk, vector))
        response = self._client.bulk(body=body, params={"refresh": "true"})
        if response.get("errors"):
            failed = [item for item in response.get("items", []) if next(iter(item.values())).get("error")]
            raise ExternalServiceError(f"OpenSearch 색인 중 {len(failed)}건 거부: {failed[:1]}")
        return len(chunks)

    def delete_by_source(self, source_ids: set[str]) -> int:
        """주어진 원문(source_id)에서 나온 청크를 모두 지우고 지운 개수를 돌려준다."""
        if not source_ids:
            return 0
        response = self._client.delete_by_query(
            index=self._index,
            body={"query": {"terms": {"source_id": sorted(source_ids)}}},
            params={"refresh": "true"},
        )
        return int(response.get("deleted", 0))

    def list_source_ids(self) -> set[str]:
        """지금 색인에 들어 있는 원문 ID 전체(source_id keyword의 terms 집계)."""
        response = self._client.search(
            index=self._index,
            body={"size": 0, "aggs": {"sources": {"terms": {"field": "source_id", "size": 10000}}}},
        )
        return {bucket["key"] for bucket in response["aggregations"]["sources"]["buckets"]}

    def health(self) -> tuple[str, int]:
        """이 색인의 상태("green"/"yellow"/"red")와 색인된 문서 수를 돌려준다(`/api/health`가 쓴다).

        색인 이름으로 좁혀 묻는다: 클러스터 전체가 아니라 우리 색인의 상태만 알면 되고, 다른 색인이 생겨도 영향받지 않는다.
        단일 노드는 복제본을 둘 곳이 없어서 정상이어도 yellow일 수 있다(그래서 yellow도 "살아 있음"으로 본다).
        `request_timeout`: 검색엔진이 멈췄을 때 상태 확인이 스레드를 오래 붙잡지 않도록 짧게 끊는다.
        응답하지 않으면 예외가 그대로 올라간다 — 그것을 "down"으로 바꾸는 일은 엔드포인트가 맡는다(어댑터가 정상을 지어내지 않는다).
        """
        status = self._client.cluster.health(index=self._index, request_timeout=_HEALTH_TIMEOUT_S)["status"]
        count = self._client.count(index=self._index, request_timeout=_HEALTH_TIMEOUT_S)["count"]
        return str(status), int(count)

    def search(
        self,
        text: str,
        vector: list[float],
        *,
        size: int = 20,
        only_current: bool = True,
    ) -> list[Hit]:
        """하이브리드 쿼리를 RRF 파이프라인으로 실행해 상위 `size`개를 1등부터 순위를 매겨 돌려준다."""
        body = build_hybrid_query(text, vector, depth=self._depth, size=size, only_current=only_current)
        response = self._client.search(
            index=self._index, body=body, params={"search_pipeline": self._pipeline}
        )
        return [
            Hit(chunk=doc_to_chunk(h["_source"]), score=float(h["_score"]), rank=rank)
            for rank, h in enumerate(response["hits"]["hits"], start=1)
        ]
