"""Gemini 어댑터: 임베딩 부분(`GeminiEmbedder`). 대화 모델(GeminiLlm)은 Task 10에서 이 파일에 더한다.

역할: 글을 `gemini-embedding-2`로 768차원 벡터로 바꾼다(EmbedderPort 구현).
사용처: 색인 파이프라인(Task 7, app/ingest/indexer.py)이 문서를, 검색 노드(Task 11)가 질문을 벡터로 만들 때.

LangChain의 임베딩 클래스를 쓰지 않고 `google.genai` 클라이언트를 직접 부르는 이유(원장 판정):
1. gemini-embedding-2는 `task_type`을 지원하지 않고 작업 지시를 프롬프트에 넣으라고 공식 문서가 안내한다.
   LangChain 클래스는 task_type이 없으면 RETRIEVAL_DOCUMENT/RETRIEVAL_QUERY를 기본으로 붙인다(embeddings.py).
2. LangChain은 429를 일반 오류(GoogleGenerativeAIError)로 감싸서 "한도 초과인지"를 구분하기 어렵다.
3. 이 키로 직접 부르는 방식이 0단계 측정(scripts/step0_probe.py)에서 12/12 성공했다.

입력 프롬프트 형식(공식 문서, https://aistudio.google.com/docs/embeddings):
- 질문:  `task: search result | query: {내용}`
- 문서:  `title: {제목} | text: {내용}` (제목이 없으면 `title: none`)
질문과 문서는 반드시 이 짝으로 써야 같은 공간에서 비교된다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from google.genai import errors as genai_errors
from google.genai import types

from app.domain.errors import QuotaExceeded

_QUERY_PREFIX = "task: search result | query: "
_DOCUMENT_PREFIX = "title: none | text: "


def parse_retry_delay(body: Any) -> float | None:
    """구글 API 오류 본문에서 `retryDelay`("34s" 형태)를 초 단위 숫자로 꺼낸다. 없거나 모양이 다르면 None.

    본문 모양(`error.details[].retryDelay`)은 구글 API 규약(google.rpc.RetryInfo)을 따른 가정이다.
    실제 Gemini 429 응답에서 확인되기 전까지는 "있으면 쓰고, 없으면 None"으로만 다룬다.
    """
    try:
        for item in body["error"]["details"]:
            delay = item.get("retryDelay")
            if isinstance(delay, str) and delay.endswith("s"):
                return float(delay[:-1])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return None


def quota_from_genai_error(exc: Exception) -> QuotaExceeded | None:
    """Gemini SDK 예외가 한도 초과(HTTP 429)면 QuotaExceeded로 바꿔 돌려주고, 아니면 None."""
    if isinstance(exc, genai_errors.ClientError) and exc.code == 429:
        return QuotaExceeded(str(exc), retry_after_s=parse_retry_delay(exc.details))
    return None


class GeminiEmbedder:
    """`gemini-embedding-2` 임베더 (EmbedderPort 구현).

    dim: 출력 차원. 모델은 기본(3072)이 아닌 차원을 스스로 정규화해 주므로 직접 정규화하지 않는다(공식 문서).
    batch_size: 요청 하나에 담는 문서 수. 입력 합계 한도(8,192토큰)에 걸리지 않는 보수적 값이다.
    max_retries: 한도 초과(429)일 때 **총 시도 횟수**(처음 시도 포함). 모두 실패하면 QuotaExceeded.
    client: 테스트용으로 바꿔 끼우는 `google.genai.Client` 모양의 객체. 없으면 api_key로 처음 쓸 때 만든다.
    sleep·backoff_s·max_wait_s: 재시도 사이의 대기. 서버가 알려 준 대기 시간(retryDelay)이 있으면 그만큼,
    없으면 backoff_s에서 두 배씩 늘린다. 대기가 max_wait_s보다 길면 붙잡혀 있지 않고 바로 QuotaExceeded를 낸다
    (무료 한도의 일 단위 소진처럼 기다려도 소용없는 경우 파이프라인이 멈춰 서는 것을 막는다).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-embedding-2",
        dim: int = 768,
        batch_size: int = 50,
        max_retries: int = 3,
        client: Any = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        backoff_s: float = 1.0,
        max_wait_s: float = 60.0,
    ) -> None:
        self.model = model
        self.dim = dim
        self._api_key = api_key
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._client = client
        self._sleep = sleep
        self._backoff_s = backoff_s
        self._max_wait_s = max_wait_s
        self.quota_errors = 0  # 지금까지 받은 한도 초과(429) 횟수. 색인 보고서(docs/step0-report.md)에 적는다.
        # task_type은 일부러 비워 둔다: embedding-2는 지원하지 않는다.
        self._config = types.EmbedContentConfig(output_dimensionality=dim)

    def _get_client(self) -> Any:
        """진짜 클라이언트는 처음 쓸 때 만든다(만들 때 네트워크를 쓰지 않지만, 테스트에서는 아예 필요 없다)."""
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """색인할 문서들을 벡터로 바꾼다. 입력 순서와 같은 순서로 돌려준다."""
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(self._embed([_DOCUMENT_PREFIX + t for t in batch]))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """검색 질문 하나를 벡터로 바꾼다."""
        return self._embed([_QUERY_PREFIX + text])[0]

    def _embed(self, prompts: list[str]) -> list[list[float]]:
        """프롬프트들을 한 요청으로 보내 벡터를 받고, 개수와 차원을 검사한다."""
        # 글마다 Content를 따로 만든다. 하나의 Content에 여러 부분(part)을 넣으면 모델이 그것을 하나의
        # 입력으로 보고 임베딩 하나로 합칠 수 있어서(멀티모달 입력 규칙) 문서와 벡터의 짝이 깨진다.
        contents = [{"parts": [{"text": p}]} for p in prompts]
        response = self._call_with_retry(contents)
        vectors = [list(e.values) for e in response.embeddings]
        if len(vectors) != len(prompts):
            raise ValueError(f"입력 {len(prompts)}개에 임베딩 {len(vectors)}개가 돌아왔습니다")
        for vector in vectors:
            if len(vector) != self.dim:
                raise ValueError(f"임베딩 차원이 {len(vector)}입니다(기대 {self.dim}). 모델·차원 설정을 확인하세요")
        return vectors

    def _call_with_retry(self, contents: list[dict]) -> Any:
        """한도 초과(429)일 때만 재시도한다. 그 밖의 오류(잘못된 요청 등)는 다시 해도 같으므로 그대로 올린다."""
        for attempt in range(1, self._max_retries + 1):
            try:
                return self._get_client().models.embed_content(
                    model=self.model, contents=contents, config=self._config
                )
            except genai_errors.ClientError as exc:
                quota = quota_from_genai_error(exc)
                if quota is None:
                    raise
                self.quota_errors += 1
                if attempt == self._max_retries:
                    raise quota from exc
                wait = quota.retry_after_s if quota.retry_after_s is not None else self._backoff_s * 2 ** (attempt - 1)
                if wait > self._max_wait_s:
                    raise quota from exc
                self._sleep(wait)
        raise AssertionError("도달할 수 없는 곳")  # max_retries가 1 이상이면 위 반복에서 반드시 반환하거나 던진다
