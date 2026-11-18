"""Gemini 어댑터: 임베딩(`GeminiEmbedder`)과 대화 모델(`GeminiLlm`) 두 가지를 한 파일에 둔다.

역할:
- GeminiEmbedder: 글을 `gemini-embedding-2`로 768차원 벡터로 바꾼다(EmbedderPort 구현).
- GeminiLlm: 시스템·사용자 프롬프트를 보내 `schema` 모양의 구조화된 답을 받는다(LlmPort 구현). 맨 아래에 있다.
사용처: 색인 파이프라인(Task 7, app/ingest/indexer.py)과 검색 노드(Task 11)가 임베더를, 질문 분석·생성·검증 노드
(Task 10·12·13)가 LLM을 쓴다.

임베더는 LangChain 클래스가 아니라 `google.genai` 클라이언트를 직접 부르고, LLM은 LangChain의
`ChatGoogleGenerativeAI`를 쓴다. 이렇게 갈린 이유가 있다 — 아래 두 설명을 차례로 읽는다.

[임베더] LangChain의 임베딩 클래스를 쓰지 않고 `google.genai` 클라이언트를 직접 부르는 이유(원장 판정):
1. gemini-embedding-2는 `task_type`을 지원하지 않고 작업 지시를 프롬프트에 넣으라고 공식 문서가 안내한다.
   LangChain 클래스는 task_type이 없으면 RETRIEVAL_DOCUMENT/RETRIEVAL_QUERY를 기본으로 붙인다(embeddings.py).
2. LangChain은 429를 일반 오류(GoogleGenerativeAIError)로 감싸서 "한도 초과인지"를 구분하기 어렵다.
3. 이 키로 직접 부르는 방식이 0단계 측정(scripts/step0_probe.py)에서 12/12 성공했다.

입력 프롬프트 형식(공식 문서, https://aistudio.google.com/docs/embeddings):
- 질문:  `task: search result | query: {내용}`
- 문서:  `title: {제목} | text: {내용}` (제목이 없으면 `title: none`)
질문과 문서는 반드시 이 짝으로 써야 같은 공간에서 비교된다.

[LLM] 대화 모델은 LangChain을 거친다. 임베더와 반대로 LangChain이 얻는 것이 있기 때문이다:
1. `with_structured_output(schema)`가 "JSON 스키마를 모델에 강제하고 결과를 pydantic 객체로 검증"까지 해 준다.
2. 호출이 LangChain의 콜백 체계를 지나가므로, Langfuse의 `CallbackHandler`가 입력·출력·토큰 사용량·지연을 자동으로
   기록한다(스펙 6.7). 직접 SDK를 부르면 이 기록을 손으로 해야 한다.
3. 호출 설정(config)의 `tags`로 "nostream" 같은 꼬리표를 달 수 있다.
대신 LangChain은 429를 일반 오류로 감싸서 올리므로(원인에 원래 예외가 매달려 있다), 아래 `_quota_from_chain`이
원인 사슬을 따라가 한도 초과인지 알아낸다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from google.genai import errors as genai_errors
from google.genai import types
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError, GoogleRateLimitError
from pydantic import BaseModel

from app.domain.errors import ExternalServiceError, QuotaExceeded

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


# 외부 서비스 쪽 문제로 보고 ExternalServiceError(또는 QuotaExceeded)로 바꿔 줄 예외들. 일부러 좁게 잡았다:
# 우리 코드의 진짜 버그(KeyError 등)까지 "외부 서비스 실패"로 위장하면 원인을 찾기 어려워진다(Task 8과 같은 원칙).
_EXTERNAL_ERRORS: tuple[type[BaseException], ...] = (
    genai_errors.APIError,  # 구글 SDK의 400대(ClientError)·500대(ServerError) 응답
    ChatGoogleGenerativeAIError,  # LangChain이 위 오류를 감싼 것(GoogleRateLimitError 등)
    httpx.HTTPError,  # 연결 실패·시간 초과 같은 전송 문제
    TimeoutError,
    ConnectionError,
)


def _quota_from_chain(exc: BaseException) -> QuotaExceeded | None:
    """예외와 그 원인 사슬(`__cause__`)을 따라가며 한도 초과(429)면 QuotaExceeded로 바꿔 돌려주고, 아니면 None.

    LangChain은 SDK의 429(ClientError)를 GoogleRateLimitError로 감싸 `raise ... from e`로 올린다. 그래서 겉 예외만
    보면 retryDelay(서버가 알려 준 대기 시간)를 놓친다. 원인 사슬에서 원래 ClientError를 먼저 찾고, 없을 때만
    "감싼 예외 자체가 한도 초과 종류인가"로 판단한다.
    """
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(chain) < 5:  # 5단계 제한: 순환 참조 같은 이상한 사슬에서 멈추기 위한 안전망
        chain.append(current)
        current = current.__cause__
    for item in chain:
        if isinstance(item, Exception) and (quota := quota_from_genai_error(item)) is not None:
            return quota
    if any(isinstance(item, GoogleRateLimitError) for item in chain):
        return QuotaExceeded(str(exc))
    return None


class GeminiLlm:
    """Gemini 대화 모델 (LlmPort 구현). 구조화 출력(JSON → pydantic 객체)을 돌려준다.

    api_key·model: 이름 그대로. 모델 이름은 Settings.llm_model에서 오고, Task 17의 모델 비교 뒤에 확정한다.
    max_retries: 한도 초과(429)일 때 **총 시도 횟수**(처음 시도 포함). GeminiEmbedder와 같은 뜻이다. 모두 실패하면 QuotaExceeded.
        429가 아닌 실패는 다시 해도 같으므로 재시도하지 않는다.
    client: 테스트용으로 바꿔 끼우는 LangChain 채팅 모델 모양의 객체(`with_structured_output`을 가진 것).
        없으면 처음 쓸 때 `ChatGoogleGenerativeAI`를 만든다.
    sleep·backoff_s·max_wait_s: 재시도 사이의 대기. 서버가 알려 준 대기 시간(retryDelay)이 있으면 그만큼, 없으면
        backoff_s에서 두 배씩 늘린다. 대기가 max_wait_s보다 길면 기다리지 않고 바로 QuotaExceeded를 낸다 — 웹 요청 하나가
        몇 분씩 매달리면 안 되기 때문이다. 기본 20초는 측정한 값이 아닌 시작값이다(실제 429의 retryDelay는 아직 못 봤다).

    last_usage: 가장 최근 호출의 토큰 사용량(`{"input_tokens":…, "output_tokens":…, "total_tokens":…}`), 없으면 None.
        Langfuse 콜백은 이것과 별개로 LangChain 응답에서 사용량을 직접 가져가므로, 이 속성은 점검·측정용 보조 기록이다.
        한 인스턴스를 동시에 여러 요청이 쓰면 마지막 호출 것이 남는다(uvicorn 워커 1개, 질문 하나씩 처리하는 현재 구조에서는 문제 없다).
    quota_errors: 지금까지 받은 한도 초과(429) 횟수.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        max_retries: int = 2,
        client: Any = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        backoff_s: float = 1.0,
        max_wait_s: float = 20.0,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries는 1 이상이어야 합니다(총 시도 횟수)")
        self.model = model
        self._api_key = api_key
        self._max_retries = max_retries
        self._client = client
        self._sleep = sleep
        self._backoff_s = backoff_s
        self._max_wait_s = max_wait_s
        self.last_usage: dict | None = None
        self.quota_errors = 0

    def _get_client(self) -> Any:
        """진짜 채팅 모델은 처음 쓸 때 만든다(만들 때 네트워크를 쓰지 않는다).

        `max_retries=1`은 "SDK 자체 재시도 없음"이다(`0`이 아니라 `1`인 이유는 구글 SDK가 0을 "기본값 사용"으로 읽기 때문).
        LangChain 기본값은 6회라서, 그대로 두면 SDK가 429를 몰래 여러 번 다시 보내고 그 위에 우리 재시도가 겹쳐
        무료 한도를 더 태우고 요청이 오래 매달린다. 재시도는 이 클래스 한 곳에서만 한다.
        temperature는 지정하지 않는다: Gemini 3 이상에서는 지정하지 않으면 LangChain이 모델 기본값을 쓴다.
        """
        if self._client is None:
            self._client = ChatGoogleGenerativeAI(model=self.model, api_key=self._api_key, max_retries=1)
        return self._client

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        *,
        tags: list[str] | None = None,
    ) -> BaseModel:
        """system/user 프롬프트를 보내고 `schema` 모양으로 검증된 객체를 받는다.

        tags: 호출 설정(config)의 꼬리표. 예: ["nostream"]. 주지 않으면 config에 태그를 넣지 않아, 그래프가 호출 환경으로
            물려준 설정(Langfuse 콜백 등)을 덮어쓰지 않는다.
        한도 초과 → QuotaExceeded(재시도 뒤), 그 밖의 외부 서비스 실패·스키마에 맞지 않는 출력 → ExternalServiceError.
        """
        # include_raw=True: 파싱된 객체뿐 아니라 모델의 원본 응답(토큰 사용량이 든 AIMessage)도 함께 받는다.
        runnable = self._get_client().with_structured_output(schema, include_raw=True)
        messages = [SystemMessage(system), HumanMessage(user)]
        config = {"tags": list(tags)} if tags else None
        result = self._invoke_with_retry(runnable, messages, config)

        usage = getattr(result.get("raw"), "usage_metadata", None)
        self.last_usage = dict(usage) if usage else None  # 파싱에 실패해도 토큰은 이미 썼으므로 먼저 기록한다.
        parsed = result.get("parsed")
        if parsed is None:
            raise ExternalServiceError(
                f"모델 출력을 {schema.__name__} 모양으로 읽지 못했습니다: {result.get('parsing_error')}"
            )
        return parsed

    def _invoke_with_retry(self, runnable: Any, messages: list, config: dict | None) -> dict:
        """한도 초과(429)일 때만 재시도한다. 그 밖의 외부 서비스 실패는 곧바로 ExternalServiceError로 바꿔 올린다."""
        for attempt in range(1, self._max_retries + 1):
            try:
                return runnable.invoke(messages, config=config)
            except _EXTERNAL_ERRORS as exc:
                quota = _quota_from_chain(exc)
                if quota is None:
                    raise ExternalServiceError(f"Gemini 호출에 실패했습니다: {exc}") from exc
                self.quota_errors += 1
                if attempt == self._max_retries:
                    raise quota from exc
                wait = quota.retry_after_s if quota.retry_after_s is not None else self._backoff_s * 2 ** (attempt - 1)
                if wait > self._max_wait_s:
                    raise quota from exc
                self._sleep(wait)
        raise AssertionError("도달할 수 없는 곳")  # max_retries가 1 이상이면 위 반복에서 반드시 반환하거나 던진다
