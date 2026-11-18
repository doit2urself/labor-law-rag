"""테스트 공용 가짜 구현체(Fake)와 설정 도우미.

왜 필요한가: Gemini·Cohere·OpenSearch 같은 외부 서비스를 부르지 않고도 그래프·API·색인 로직을
이 PC에서 빠르게 시험하기 위해, `app/ports.py`의 인터페이스(포트)를 메모리 안에서 흉내 낸다.
진짜 어댑터는 `app/adapters/`에 있고, 서버에서만 `-m integration`으로 따로 시험한다.

사용처: 모든 테스트가 `from tests.conftest import FakeLlm, settings_factory` 형태로 가져다 쓴다.
"""

from __future__ import annotations

import dataclasses
import hashlib
from dataclasses import dataclass
from typing import Any

import bcrypt
import pytest
from pydantic import BaseModel

from app.config import Settings
from app.domain.models import Chunk, Hit, RerankResult

# 로그인 시험에 쓰는 비밀번호와 그 bcrypt 해시. 앱은 시작할 때 해시 형식을 검사하므로(깨진 해시로 조용히 도는 것을 막으려고)
# 가짜 문자열이 아니라 진짜 해시여야 한다. rounds=4는 bcrypt가 허용하는 최소 비용이라 테스트가 빨리 돈다(운영 기본값은 12).
ADMIN_PASSWORD = "admin-pw-for-tests"
TEST_PASSWORD = "test-pw-for-tests"
ADMIN_HASH = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()
TEST_HASH = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


def settings_factory(**overrides: Any) -> Settings:
    """테스트용 Settings를 만든다.

    필수 항목(키·해시 등)에는 가짜 값을 채우고 `.env` 파일은 읽지 않는다.
    `overrides`로 원하는 항목만 바꿀 수 있다. 예: `settings_factory(test_ip_limit=3)`.
    """
    values: dict[str, Any] = {
        "law_api_oc": "test-oc",
        "gemini_api_key": "test-gemini-key",
        "cohere_api_key": "test-cohere-key",
        "opensearch_password": "test-opensearch-password",
        "admin_password_hash": ADMIN_HASH,
        "test_password_hash": TEST_HASH,
        "session_secret": "test-session-secret",
        "langfuse_public_key": "test-langfuse-public",
        "langfuse_secret_key": "test-langfuse-secret",
        "langfuse_host": "https://langfuse.invalid",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeEmbedder:
    """텍스트마다 항상 같은 가짜 벡터를 돌려주는 임베더 (EmbedderPort).

    `queries`: embed_query로 받은 글의 기록. 검색 노드가 "어떤 검색어를 벡터로 바꿨는지" 확인하는 데 쓴다.
    """

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim
        self.queries: list[str] = []

    def _vector(self, text: str) -> list[float]:
        # 같은 글이면 같은 벡터가 나와야 "멱등" 같은 성질을 시험할 수 있다.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self.dim)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return self._vector(text)


@dataclass
class RecordedPrompt:
    """FakeLlm이 받은 호출 한 건의 기록. 테스트가 프롬프트 내용을 검사하는 데 쓴다."""

    system: str
    user: str
    schema: type[BaseModel]
    tags: list[str] | None


class FakeLlm:
    """미리 넣어 둔 응답을 차례로 돌려주고, 받은 프롬프트를 기록하는 LLM (LlmPort).

    `prompts`: 호출마다의 `RecordedPrompt`. `call_count`: 지금까지 호출 수.
    응답이 모자라면 조용히 넘어가지 않고 AssertionError를 내서 테스트가 실패하게 한다.
    """

    def __init__(self, responses: list[BaseModel] | None = None) -> None:
        self._responses = list(responses or [])
        self.prompts: list[RecordedPrompt] = []

    @property
    def call_count(self) -> int:
        return len(self.prompts)

    @classmethod
    def script(cls, **scenario: Any) -> "ScriptedLlm":
        """그래프 전체를 돌리는 시험용: 스키마(Analysis·Sufficiency·GeneratedAnswer…)를 보고 알맞은 응답을 스스로 고르는 가짜.

        시나리오 인자는 `ScriptedLlm`의 설명을 본다. 응답을 순서대로 미리 넣는 일반 `FakeLlm([...])`과 달리,
        그래프가 재생성·재검색으로 몇 번 돌든 응답이 모자라지 않는다.
        """
        return ScriptedLlm(**scenario)

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        tags: list[str] | None = None,
    ) -> BaseModel:
        self.prompts.append(RecordedPrompt(system, user, schema, tags))
        if not self._responses:
            raise AssertionError("FakeLlm: 준비된 응답이 모두 소진되었습니다")
        response = self._responses.pop(0)
        if isinstance(response, Exception):  # 응답 자리에 예외 객체를 넣으면 그 순서의 호출이 실패한다(한도 초과 등 시험용)
            raise response
        return response


class ScriptedLlm(FakeLlm):
    """스키마 종류에 따라 응답을 만들어 주는 시나리오형 가짜 LLM. 호출 기록(prompts, call_count)은 FakeLlm과 같다.

    시나리오 인자:
    - qclass: 질문 분석이 내릴 분류(기본 "counsel").
    - first_answer_with_text / first_answer_citations: 첫 번째 답변의 글과 인용 ID(기본 글은 평범한 문장, 인용 ["doc1"]).
      이후 답변은 항상 평범한 문장 + ["doc1"]이다.
    - verification_fails_once: 첫 번째 검증만 "근거 불일치"로 답한다. always_unsupported: 검증이 항상 불일치.
    - verdict_found_once: 첫 번째 검증이 "사건 판단 문장 있음"으로 답한다.
    답변은 언제나 문장 1개라서 검증 응답의 sentences_ok도 1개다.
    """

    def __init__(
        self,
        *,
        qclass: str = "counsel",
        first_answer_with_text: str | None = None,
        first_answer_citations: list[str] | None = None,
        verification_fails_once: bool = False,
        always_unsupported: bool = False,
        verdict_found_once: bool = False,
    ) -> None:
        super().__init__([])
        self._qclass = qclass
        self._first_text = first_answer_with_text
        self._first_citations = first_answer_citations
        self._fails_once = verification_fails_once
        self._always_unsupported = always_unsupported
        self._verdict_once = verdict_found_once
        self._seen: dict[str, int] = {}

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        tags: list[str] | None = None,
    ) -> BaseModel:
        # 순환 import를 피하려고 여기서 불러온다(app.graph는 tests/conftest보다 나중에 만들어진 모듈이다).
        from app.domain.models import GeneratedAnswer, SentenceOut, Verification
        from app.graph.nodes_analyze import Analysis
        from app.graph.nodes_search import Refined, Sufficiency

        self.prompts.append(RecordedPrompt(system, user, schema, tags))
        n = self._seen.get(schema.__name__, 0)  # 이 스키마로 지금까지 몇 번째 호출인지(0부터)
        self._seen[schema.__name__] = n + 1

        if schema is Analysis:
            return Analysis(rewritten_query="주휴수당 지급 요건", qclass=self._qclass)  # type: ignore[arg-type]
        if schema is Sufficiency:
            return Sufficiency(sufficient=True)
        if schema is Refined:
            return Refined(query=f"새 검색어 {n + 1}")
        if schema is GeneratedAnswer:
            first = n == 0
            text = self._first_text if first and self._first_text else "주휴수당은 유급휴일에 대한 임금입니다."
            citations = self._first_citations if first and self._first_citations is not None else ["doc1"]
            return GeneratedAnswer(sentences=[SentenceOut(text=text, citation_ids=citations, kind="evidence")])
        if schema is Verification:
            unsupported = self._always_unsupported or (self._fails_once and n == 0)
            return Verification(
                sentences_ok=[not unsupported],
                verdict_found=self._verdict_once and n == 0,
                notes="근거 불일치" if unsupported else "",
            )
        raise AssertionError(f"ScriptedLlm: 처리할 수 없는 스키마 {schema.__name__}")


def sample_hits(n: int = 5) -> list[Hit]:
    """그래프 시험용 기본 검색 결과: 근로기준법 제51~(50+n)조, 점수는 0.9에서 0.1씩 내려간다."""
    hits = []
    for i in range(1, n + 1):
        chunk = Chunk(
            chunk_id=f"law:근로기준법:1:20260101:{50 + i}:0",
            doc_type="law",
            title=f"근로기준법 제{50 + i}조(휴일)",
            text=f"제{50 + i}조(휴일) 사용자는 근로자에게 1주에 평균 1회 이상의 유급휴일을 보장하여야 한다.",
            law_name="근로기준법",
            article_no=str(50 + i),
            paragraph=None,
            effective_date="20260101",
            promulgation_date=None,
            source_id="law:근로기준법:1:20260101",
            source_url=f"https://www.law.go.kr/법령/근로기준법/제{50 + i}조",
            extra={},
        )
        hits.append(Hit(chunk, round(0.9 - 0.1 * (i - 1), 2), i))
    return hits


class FakeReranker:
    """상위 `top_n`개를 고르고 순위를 1부터 다시 매기는 리랭커 (RerankerPort).

    `raises`를 주면 호출할 때 그 예외를 낸다(한도 초과 같은 실패 경로 시험용).
    """

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls = 0

    def rerank(self, query: str, hits: list[Hit], top_n: int) -> list[Hit]:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        picked = hits[:top_n]
        return [
            dataclasses.replace(h, rank=i, score=1.0 - 0.1 * i)
            for i, h in enumerate(picked, start=1)
        ]


class FakeRerankStage:
    """RerankStage 가짜: 상위 `top_n`개를 순위 1부터 다시 매겨 돌려준다.

    `used_reranker`/`reason`을 정해 두면 "한도 도달(cap)"·"오류(error)" 폴백 경로도 시험할 수 있다.
    진짜 RerankGate(월 한도 게이트)는 tests/test_rerank_gate.py에서 따로 시험한다.
    """

    def __init__(self, *, used_reranker: bool = True, reason: str = "ok") -> None:
        self._used_reranker = used_reranker
        self._reason = reason
        self.calls = 0

    def rerank(self, query: str, hits: list[Hit], top_n: int = 5) -> RerankResult:
        self.calls += 1
        picked = [dataclasses.replace(h, rank=i) for i, h in enumerate(hits[:top_n], start=1)]
        return RerankResult(picked, self._used_reranker, self._reason)  # type: ignore[arg-type]


class FakeSearch:
    """생성자에 넣어 둔 후보를 그대로 돌려주는 검색기 (SearchPort).

    `calls`로 검색이 몇 번 불렸는지 확인한다(예: 범위 밖 질문은 검색을 건너뛰어야 한다).
    """

    def __init__(self, hits: list[Hit]) -> None:
        self._hits = list(hits)
        self.calls = 0
        self.last_args: dict[str, Any] | None = None

    def search(
        self,
        text: str,
        vector: list[float],
        *,
        size: int = 20,
        only_current: bool = True,
    ) -> list[Hit]:
        self.calls += 1
        self.last_args = {"text": text, "size": size, "only_current": only_current}
        return self._hits[:size]


class FakeIndex:
    """메모리에만 저장하는 색인 (IndexPort). `chunk_id`가 같으면 덮어쓴다."""

    def __init__(self) -> None:
        self.dim: int | None = None
        self._docs: dict[str, tuple[Chunk, list[float]]] = {}

    def ensure_index(self, dim: int) -> None:
        self.dim = dim

    def bulk_upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        for chunk, vector in zip(chunks, vectors, strict=True):
            self._docs[chunk.chunk_id] = (chunk, vector)
        return len(chunks)

    def delete_by_source(self, source_ids: set[str]) -> int:
        doomed = [cid for cid, (c, _) in self._docs.items() if c.source_id in source_ids]
        for cid in doomed:
            del self._docs[cid]
        return len(doomed)

    def list_source_ids(self) -> set[str]:
        return {c.source_id for c, _ in self._docs.values()}

    # 아래 둘은 포트에는 없고 테스트가 상태를 들여다보기 위한 편의 메서드다.
    def count(self) -> int:
        return len(self._docs)

    def get(self, chunk_id: str) -> Chunk:
        return self._docs[chunk_id][0]


class RecordingSink:
    """그래프가 남기는 수동 기록 이벤트(EventSink)를 받아 적어 두는 가짜. 시험이 `events`를 들여다본다.

    `events`는 `{"name": 이름, "payload": 내용}` 목록이다. 내용은 그래프가 마스킹을 거친 뒤 넘긴 값이다.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def __call__(self, name: str, payload: dict) -> None:
        self.events.append({"name": name, "payload": payload})

    def names(self) -> list[str]:
        return [event["name"] for event in self.events]

    def payloads(self, name: str) -> list[dict]:
        return [event["payload"] for event in self.events if event["name"] == name]


def deps_with(llm: Any, **overrides: Any) -> Any:
    """그래프 노드 테스트용 `GraphDeps`를 만든다. 넘기지 않은 의존성은 가짜 구현으로 채운다.

    예: `deps_with(FakeLlm([...]), search=FakeSearch(hits))`처럼 시험에 필요한 것만 바꿔 끼운다.
    `app.graph`는 Task 10에서 생기므로 import를 함수 안에 둔다(그래야 이 파일을 쓰는 다른 테스트가 영향받지 않는다).
    """
    from app.graph.deps import GraphDeps
    from app.scope import COVERED_LAWS

    values: dict[str, Any] = {
        "llm": llm,
        "embedder": FakeEmbedder(),
        "search": FakeSearch(sample_hits()),
        "rerank": FakeRerankStage(),
        "settings": settings_factory(),
        "covered_laws": list(COVERED_LAWS),
    }
    values.update(overrides)
    return GraphDeps(**values)


# ---- API 테스트용 픽스처 (Task 14) ----
# import를 함수 안에 두는 이유: app.api는 Task 14에서 생겼고, 이 파일을 쓰는 다른 테스트(그래프·색인)가 FastAPI를
# 불러오느라 느려지지 않게 하려는 것이다.


@pytest.fixture
def api_app(tmp_path):
    """가짜 LLM·검색·리랭커로 만든 앱(로그인한 계정·SQLite는 tmp_path 안). 그래프는 진짜 LangGraph이고 부품만 가짜다."""
    from tests.api_support import make_app

    return make_app(tmp_path)


@pytest.fixture
async def admin_cookie(api_app) -> str:
    """`admin`으로 로그인해 얻은 세션 쿠키 값."""
    from tests.api_support import login_cookie

    return await login_cookie(api_app, "admin", ADMIN_PASSWORD)


@pytest.fixture
async def test_cookie(api_app) -> str:
    """`test`로 로그인해 얻은 세션 쿠키 값."""
    from tests.api_support import login_cookie

    return await login_cookie(api_app, "test", TEST_PASSWORD)
