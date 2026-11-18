"""GeminiEmbedder 단위 테스트: 가짜 `google.genai` 클라이언트로 요청 모양·재시도·검사를 시험한다(네트워크 없음).

진짜 Gemini 호출은 0단계 측정(scripts/step0_probe.py)과 서버 색인(scripts/build_index.py)에서 한다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai import errors as genai_errors

from app.adapters.gemini import GeminiEmbedder
from app.domain.errors import QuotaExceeded
from app.ports import EmbedderPort


def quota_error(retry_delay: str | None = None) -> genai_errors.ClientError:
    """HTTP 429(한도 초과)를 흉내 낸 SDK 예외. retry_delay가 있으면 구글 규약처럼 details에 담는다."""
    details = [{"retryDelay": retry_delay}] if retry_delay else []
    body = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": details}}
    return genai_errors.ClientError(429, body)


class FakeGenaiClient:
    """`client.models.embed_content(model=, contents=, config=)`만 가진 가짜 google.genai 클라이언트.

    outcomes: 호출마다 차례로 쓸 결과 목록. 예외 객체면 던지고, None이면 정상 응답을 만든다. 다 쓰면 정상 응답.
    각 정상 응답은 입력마다 `dim`차원 벡터를 하나씩 준다(첫 값이 입력의 글자 수라서 입력 순서를 확인할 수 있다).
    """

    def __init__(self, *, dim: int = 768, outcomes: list | None = None, short_by: int = 0) -> None:
        self.dim = dim
        self.calls = 0
        self.requests: list[dict] = []
        self._outcomes = list(outcomes or [])
        self._short_by = short_by  # 응답이 입력보다 몇 개 모자라게 돌려줄지(입력 합쳐짐 사고 흉내)
        self.models = SimpleNamespace(embed_content=self._embed_content)

    def _embed_content(self, *, model, contents, config):
        self.calls += 1
        self.requests.append({"model": model, "contents": contents, "config": config})
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if outcome is not None:
            raise outcome
        n = len(contents) - self._short_by
        texts = [c["parts"][0]["text"] for c in contents][:n]
        vectors = [SimpleNamespace(values=[float(len(t))] + [0.5] * (self.dim - 1)) for t in texts]
        return SimpleNamespace(embeddings=vectors)

    def sent_texts(self) -> list[list[str]]:
        """요청마다 실제로 보낸 글(접두어 포함)."""
        return [[c["parts"][0]["text"] for c in r["contents"]] for r in self.requests]


class Sleeps:
    """대기 시간을 실제로 자지 않고 기록만 하는 sleep 대용품."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def make(client, **kwargs) -> tuple[GeminiEmbedder, Sleeps]:
    sleeps = Sleeps()
    return GeminiEmbedder("k", client=client, sleep=sleeps, **kwargs), sleeps


# 반환 벡터 길이가 설정한 차원(768)과 다르면 조용히 넘어가지 않고 오류를 낸다(색인 매핑과 어긋나는 사고 방지).
def test_embedder_checks_dimension():
    embedder, _ = make(FakeGenaiClient(dim=3072))
    with pytest.raises(ValueError):
        embedder.embed_query("x")


# 한도 초과가 재시도(3번) 뒤에도 이어지면 QuotaExceeded. 총 시도는 정확히 3번이다.
def test_embedder_raises_quota_after_retries():
    client = FakeGenaiClient(outcomes=[quota_error()] * 5)
    embedder, sleeps = make(client, max_retries=3)
    with pytest.raises(QuotaExceeded):
        embedder.embed_documents(["a"])
    assert client.calls == 3 and len(sleeps.waits) == 2  # 마지막 시도 뒤에는 기다리지 않는다


# 한 번 막혔다가 풀리면 재시도로 성공하고, 서버가 알려 준 대기 시간(retryDelay)만큼 쉰다.
def test_embedder_recovers_after_a_quota_error_and_honours_retry_delay():
    client = FakeGenaiClient(outcomes=[quota_error("12s")])
    embedder, sleeps = make(client)
    assert len(embedder.embed_documents(["a"])[0]) == 768
    assert client.calls == 2 and sleeps.waits == [12.0]
    assert embedder.quota_errors == 1  # 보고서에 적을 "한도 초과(429) 발생 횟수"


# 대기 시간 힌트가 없으면 1초, 2초처럼 두 배씩 늘려 기다린다.
def test_embedder_backs_off_exponentially_without_a_hint():
    client = FakeGenaiClient(outcomes=[quota_error(), quota_error()])
    embedder, sleeps = make(client, max_retries=3)
    embedder.embed_query("x")
    assert sleeps.waits == [1.0, 2.0]


# 서버가 너무 오래(60초 초과) 기다리라고 하면 붙잡혀 있지 않고 바로 QuotaExceeded로 알린다(대기 시간 포함).
def test_embedder_does_not_wait_for_a_very_long_retry_delay():
    client = FakeGenaiClient(outcomes=[quota_error("3600s")])
    embedder, sleeps = make(client, max_retries=3)
    with pytest.raises(QuotaExceeded) as info:
        embedder.embed_query("x")
    assert info.value.retry_after_s == 3600.0 and client.calls == 1 and sleeps.waits == []


# 한도 초과가 아닌 오류(예: 400 잘못된 요청)는 재시도하지 않고 그대로 올린다.
def test_embedder_does_not_retry_other_errors():
    bad = genai_errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})
    client = FakeGenaiClient(outcomes=[bad])
    embedder, sleeps = make(client)
    with pytest.raises(genai_errors.ClientError):
        embedder.embed_query("x")
    assert client.calls == 1 and sleeps.waits == []


# 작업 지시는 task_type 필드가 아니라 프롬프트에 넣는다(gemini-embedding-2 공식 문서). 질의와 문서의 접두어가 다르다.
def test_prompts_follow_embedding_2_format_and_no_task_type():
    client = FakeGenaiClient()
    embedder, _ = make(client)
    embedder.embed_query("주휴수당 받나요")
    embedder.embed_documents(["제55조(휴일) …"])
    assert client.sent_texts() == [["task: search result | query: 주휴수당 받나요"], ["title: none | text: 제55조(휴일) …"]]
    for request in client.requests:
        assert request["model"] == "gemini-embedding-2"
        assert request["config"].output_dimensionality == 768 and request["config"].task_type is None


# 문서마다 따로 Content로 보낸다(여러 입력이 하나의 임베딩으로 합쳐지는 사고를 피하기 위한 안전한 방식).
def test_each_document_is_its_own_content():
    client = FakeGenaiClient()
    make(client)[0].embed_documents(["a", "b"])
    assert [len(c["parts"]) for c in client.requests[0]["contents"]] == [1, 1]


# 배치로 나눠 보내되(50개씩) 결과 순서는 입력 순서와 같다.
def test_batches_and_preserves_order():
    texts = ["x" * n for n in range(1, 121)]  # 글자 수가 1..120이라 첫 벡터 값으로 순서를 확인한다
    client = FakeGenaiClient()
    vectors = make(client, batch_size=50)[0].embed_documents(texts)
    assert [len(batch) for batch in client.sent_texts()] == [50, 50, 20]
    # 접두어가 붙어서 나가므로 첫 값 = 접두어 길이 + 원문 글자 수
    prefix = len("title: none | text: ")
    assert [v[0] for v in vectors] == [float(prefix + n) for n in range(1, 121)]


# 입력과 응답의 개수가 다르면(입력이 합쳐졌다는 뜻) 오류를 낸다.
def test_embedder_rejects_a_response_with_the_wrong_count():
    embedder, _ = make(FakeGenaiClient(short_by=1))
    with pytest.raises(ValueError):
        embedder.embed_documents(["a", "b"])


def test_empty_input_makes_no_call():
    client = FakeGenaiClient()
    assert make(client)[0].embed_documents([]) == [] and client.calls == 0


def test_embedder_satisfies_the_port():
    assert isinstance(make(FakeGenaiClient())[0], EmbedderPort)
