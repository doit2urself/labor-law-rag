"""0단계 한도 측정 도구(scripts/step0_probe.py)의 동작을 검증한다.

네트워크는 쓰지 않는다. 실제 Gemini·Cohere 호출은 Task 2의 Step 5에서 사람이 키를 넣고 따로 돌린다.
SDK 클라이언트 자리에는 같은 메서드 이름을 가진 가짜 객체를 넣고, 오류는 SDK의 진짜 예외 클래스로 만든다.
"""

import cohere
import pytest
from google.genai import errors as genai_errors

from app.domain.errors import ExternalServiceError, QuotaExceeded
from scripts.step0_probe import (
    build_cohere_rerank_call,
    build_gemini_chat_call,
    build_gemini_embed_call,
    main,
    probe_limit,
)


# 처음 한도 초과(QuotaExceeded)가 나오면 거기서 멈추고, 성공 횟수와 몇 번째에서 막혔는지 기록한다.
def test_probe_stops_at_first_quota_error():
    calls = iter([None, None, None, QuotaExceeded()])

    def call():
        x = next(calls)
        if x:
            raise x

    r = probe_limit(call, max_calls=60)
    assert (r.calls_ok, r.first_429_at) == (3, 4)


# 한도에 걸리지 않으면 max_calls에서 멈춘다(무료 한도를 불필요하게 쓰지 않기 위함).
def test_probe_respects_max_calls():
    assert probe_limit(lambda: None, max_calls=7).calls_ok == 7


# 429 응답이 알려 준 "다시 시도까지 대기 시간"을 결과에 남긴다. 없으면 None.
def test_probe_keeps_retry_after_from_quota_error():
    def call():
        raise QuotaExceeded(retry_after_s=12.0)

    assert probe_limit(call, max_calls=5).retry_after_s == 12.0
    assert probe_limit(lambda: None, max_calls=2).retry_after_s is None


# 소요 시간은 주입한 시계로 잰다(진짜 시간을 기다리지 않고 시험하기 위함).
def test_probe_measures_elapsed_with_injected_clock():
    now = [100.0]

    def call():
        now[0] += 1.5

    r = probe_limit(call, max_calls=3, clock=lambda: now[0])
    assert r.elapsed_s == pytest.approx(4.5)


# 한도 초과가 아닌 오류(키 오류, 모델 없음 등)는 삼키지 않고 그대로 올려서 사람이 보게 한다.
def test_probe_lets_other_errors_through():
    def call():
        raise ExternalServiceError("model not found")

    with pytest.raises(ExternalServiceError):
        probe_limit(call, max_calls=3)


class FakeGeminiClient:
    """google-genai의 `client.models.generate_content/embed_content`와 같은 이름의 메서드만 가진 가짜."""

    def __init__(self, error: Exception | None = None):
        self.models = self  # SDK는 client.models 아래에 메서드가 있다
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(("chat", model))
        if self._error:
            raise self._error

    def embed_content(self, *, model, contents, config=None):
        self.calls.append(("embed", model))
        if self._error:
            raise self._error


def _gemini_429() -> genai_errors.ClientError:
    # 응답 JSON 모양은 구글 API 규약(google.rpc.RetryInfo)을 따른 가정이다. 실제 응답은 Step 5에서 확인한다.
    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "quota",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "34s"}
            ],
        }
    }
    return genai_errors.ClientError(429, body)


# Gemini의 429는 QuotaExceeded로 바뀌고, 응답의 retryDelay("34s")가 초 단위 숫자로 남는다.
@pytest.mark.parametrize("builder", [build_gemini_chat_call, build_gemini_embed_call])
def test_gemini_429_becomes_quota_exceeded_with_retry_delay(builder):
    call = builder(FakeGeminiClient(error=_gemini_429()), "some-model")
    with pytest.raises(QuotaExceeded) as info:
        call()
    assert info.value.retry_after_s == 34.0


# 429가 아닌 Gemini 오류(예: 400 잘못된 요청)는 한도 문제가 아니므로 원래 예외 그대로 올린다.
@pytest.mark.parametrize("builder", [build_gemini_chat_call, build_gemini_embed_call])
def test_gemini_other_client_error_passes_through(builder):
    call = builder(FakeGeminiClient(error=genai_errors.ClientError(400, {"error": {}})), "m")
    with pytest.raises(genai_errors.ClientError):
        call()


# 호출은 요청한 모델 이름으로 나간다(후보 모델별 측정이 서로 섞이지 않아야 한다).
def test_gemini_calls_use_requested_model():
    client = FakeGeminiClient()
    build_gemini_chat_call(client, "gemini-chat-x")()
    build_gemini_embed_call(client, "gemini-embed-y")()
    assert client.calls == [("chat", "gemini-chat-x"), ("embed", "gemini-embed-y")]


class FakeCohereClient:
    """cohere.ClientV2.rerank와 같은 이름·인자를 가진 가짜."""

    def __init__(self, error: Exception | None = None):
        self._error = error
        self.calls: list[str] = []

    def rerank(self, *, model, query, documents, top_n=None):
        self.calls.append(model)
        if self._error:
            raise self._error


# Cohere의 TooManyRequestsError(429)는 QuotaExceeded로 바뀐다.
def test_cohere_too_many_requests_becomes_quota_exceeded():
    client = FakeCohereClient(error=cohere.TooManyRequestsError(body={"message": "rate limit"}))
    with pytest.raises(QuotaExceeded):
        build_cohere_rerank_call(client, "rerank-x")()


# 정상 호출은 요청한 모델 이름을 그대로 쓴다.
def test_cohere_call_uses_requested_model():
    client = FakeCohereClient()
    build_cohere_rerank_call(client, "rerank-x")()
    assert client.calls == ["rerank-x"]


# CLI: 결과를 한 줄로 출력하고, API 키 값은 출력 어디에도 나오지 않아야 한다.
def test_main_prints_result_and_never_prints_the_key(capsys):
    key = "SECRET-KEY-XYZ"
    code = main(
        ["gemini-chat", "--model", "m", "--max-calls", "3"],
        env={"LABOR_GEMINI_API_KEY": key},
        factories={"gemini-chat": lambda api_key, model: (lambda: None)},
    )
    out = capsys.readouterr()
    assert code == 0 and "calls_ok=3" in out.out
    assert key not in out.out and key not in out.err


# 키가 환경 변수에 없으면 어떤 변수를 채워야 하는지 알려 주고 종료 코드 2로 끝난다(호출하지 않는다).
def test_main_without_key_names_the_env_var(capsys):
    code = main(["cohere-rerank", "--model", "m"], env={}, factories={})
    assert code == 2 and "LABOR_COHERE_API_KEY" in capsys.readouterr().err


# 한도 초과가 아닌 오류는 종류와 메시지를 보여 주고 종료 코드 1로 끝난다(측정표에 "거부됨"으로 적을 근거).
def test_main_reports_non_quota_error(capsys):
    def factory(api_key, model):
        def call():
            raise ExternalServiceError("model not available on free tier")

        return call

    code = main(
        ["gemini-chat", "--model", "m"],
        env={"LABOR_GEMINI_API_KEY": "k"},
        factories={"gemini-chat": factory},
    )
    out = capsys.readouterr().out
    assert code == 1 and "ExternalServiceError" in out and "free tier" in out
