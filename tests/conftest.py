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

from pydantic import BaseModel

from app.config import Settings
from app.domain.models import Chunk, Hit


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
        "admin_password_hash": "test-admin-hash",
        "test_password_hash": "test-test-hash",
        "session_secret": "test-session-secret",
        "langfuse_public_key": "test-langfuse-public",
        "langfuse_secret_key": "test-langfuse-secret",
        "langfuse_host": "https://langfuse.invalid",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeEmbedder:
    """텍스트마다 항상 같은 가짜 벡터를 돌려주는 임베더 (EmbedderPort)."""

    def __init__(self, dim: int = 768) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        # 같은 글이면 같은 벡터가 나와야 "멱등" 같은 성질을 시험할 수 있다.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(self.dim)]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
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
        return self._responses.pop(0)


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
