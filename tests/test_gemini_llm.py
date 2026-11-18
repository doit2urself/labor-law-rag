"""GeminiLlm 단위 테스트: 가짜 LangChain 채팅 모델로 재시도·오류 변환·태그 전달을 시험한다(네트워크 없음).

진짜 Gemini 호출은 Task 17의 모델 비교와 서버 점검에서 한다.
"""

from __future__ import annotations

import httpx
import pytest
from google.genai import errors as genai_errors
from langchain_core.messages import AIMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import GoogleRateLimitError
from pydantic import BaseModel

from app.adapters.gemini import GeminiLlm
from app.domain.errors import ExternalServiceError, QuotaExceeded
from app.graph.nodes_analyze import Analysis
from app.ports import LlmPort

OK = Analysis(rewritten_query="주휴수당", qclass="counsel")
USAGE = {"input_tokens": 120, "output_tokens": 30, "total_tokens": 150}


def quota_error(retry_delay: str | None = None) -> genai_errors.ClientError:
    """HTTP 429(한도 초과)를 흉내 낸 구글 SDK 예외. retry_delay가 있으면 구글 규약처럼 details에 담는다."""
    details = [{"retryDelay": retry_delay}] if retry_delay else []
    body = {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED", "details": details}}
    return genai_errors.ClientError(429, body)


def langchain_wrapped_quota_error(retry_delay: str | None = None) -> GoogleRateLimitError:
    """진짜 langchain-google-genai가 429를 감싸는 모양(GoogleRateLimitError, 원인에 SDK 예외가 매달림)."""
    wrapped = GoogleRateLimitError("Error calling model 'm' (RESOURCE_EXHAUSTED)")
    wrapped.__cause__ = quota_error(retry_delay)
    return wrapped


class FakeChat:
    """`with_structured_output(schema, include_raw=True).invoke(messages, config=...)`만 가진 가짜 채팅 모델.

    outcomes: 호출마다 차례로 쓸 결과. 예외 객체면 던지고, 모델 객체면 파싱 성공 응답으로, dict면 그대로 돌려준다.
    다 쓰면 마지막 결과를 계속 쓴다. 호출 수·마지막 입력·마지막 config를 기록한다.
    """

    def __init__(self, outcomes: list | None = None) -> None:
        self._outcomes = list(outcomes if outcomes is not None else [OK])
        self.calls = 0
        self.schema: type[BaseModel] | None = None
        self.include_raw: bool | None = None
        self.last_messages: list | None = None
        self.last_config: dict | None = None

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        self.include_raw = kwargs.get("include_raw")
        return self

    def invoke(self, messages, config=None):
        self.calls += 1
        self.last_messages, self.last_config = messages, config
        outcome = self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, dict):
            return outcome
        return {"raw": AIMessage(content="", usage_metadata=USAGE), "parsed": outcome, "parsing_error": None}


class Sleeps:
    """대기 시간을 실제로 자지 않고 기록만 하는 sleep 대용품."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def make(chat: FakeChat, **kwargs) -> tuple[GeminiLlm, Sleeps]:
    sleeps = Sleeps()
    return GeminiLlm("k", "m", client=chat, sleep=sleeps, **kwargs), sleeps


# 가짜도 진짜도 같은 약속(LlmPort)을 지킨다: 그래프는 이 약속만 안다.
def test_gemini_llm_satisfies_the_port():
    assert isinstance(make(FakeChat())[0], LlmPort)


# 정상 응답이면 파싱된 모델 객체를 돌려주고, 시스템·사용자 메시지를 이 순서로 보내며 구조화 출력을 요청한다.
def test_returns_parsed_model_and_sends_system_then_user():
    chat = FakeChat()
    llm, _ = make(chat)
    result = llm.complete_json("시스템 지시", "사용자 입력", Analysis)
    assert result == OK
    assert [(m.type, m.content) for m in chat.last_messages] == [("system", "시스템 지시"), ("human", "사용자 입력")]
    assert chat.schema is Analysis and chat.include_raw is True  # include_raw: 토큰 사용량이 든 원본도 받는다


# 한도 초과(429)가 계속되면 정해진 횟수만 시도한 뒤 QuotaExceeded. 마지막 시도 뒤에는 기다리지 않는다.
def test_gemini_llm_maps_429_to_quota_exceeded():
    chat = FakeChat([quota_error()])
    llm, sleeps = make(chat, max_retries=2)
    with pytest.raises(QuotaExceeded):
        llm.complete_json("s", "u", Analysis)
    assert chat.calls == 2 and len(sleeps.waits) == 1


# 진짜 스택에서는 429가 LangChain 예외로 감싸져 올라온다. 감싼 것도 한도 초과로 알아봐야 한다.
def test_langchain_wrapped_429_is_also_a_quota_error():
    chat = FakeChat([langchain_wrapped_quota_error()])
    llm, _ = make(chat, max_retries=2)
    with pytest.raises(QuotaExceeded):
        llm.complete_json("s", "u", Analysis)
    assert chat.calls == 2


# 한 번 막혔다가 풀리면 재시도로 성공하고, 서버가 알려 준 대기 시간(retryDelay)만큼 쉰다.
def test_recovers_after_one_quota_error_and_honours_retry_delay():
    chat = FakeChat([langchain_wrapped_quota_error("7s"), OK])
    llm, sleeps = make(chat, max_retries=2)
    assert llm.complete_json("s", "u", Analysis) == OK
    assert chat.calls == 2 and sleeps.waits == [7.0]


# 대기 시간이 너무 길면(웹 요청이 매달리면 안 된다) 기다리지 않고 바로 QuotaExceeded로 끝낸다.
def test_too_long_retry_delay_fails_fast_without_sleeping():
    chat = FakeChat([quota_error("300s")])
    llm, sleeps = make(chat, max_retries=3, max_wait_s=20)
    with pytest.raises(QuotaExceeded) as info:
        llm.complete_json("s", "u", Analysis)
    assert chat.calls == 1 and sleeps.waits == [] and info.value.retry_after_s == 300.0


# 한도 초과가 아닌 외부 서비스 실패(잘못된 요청 400, 서버 오류 500, 연결 문제)는 다시 해도 소용없으니 재시도 없이
# ExternalServiceError로 바꾼다.
@pytest.mark.parametrize(
    "failure",
    [
        genai_errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}}),
        genai_errors.ServerError(500, {"error": {"code": 500, "message": "boom", "status": "INTERNAL"}}),
        httpx.ConnectTimeout("시간 초과"),
    ],
    ids=["400", "500", "timeout"],
)
def test_other_external_failures_become_external_service_error(failure):
    chat = FakeChat([failure])
    llm, sleeps = make(chat, max_retries=3)
    with pytest.raises(ExternalServiceError):
        llm.complete_json("s", "u", Analysis)
    assert chat.calls == 1 and sleeps.waits == []


# 우리 코드의 진짜 버그(여기서는 KeyError)는 외부 서비스 실패로 위장하지 않고 그대로 드러난다.
def test_programming_errors_are_not_disguised():
    llm, _ = make(FakeChat([KeyError("oops")]))
    with pytest.raises(KeyError):
        llm.complete_json("s", "u", Analysis)


# 모델이 스키마에 맞지 않는 출력을 내서 파싱에 실패하면(parsed 없음) ExternalServiceError.
def test_unparseable_output_becomes_external_service_error():
    broken = {"raw": AIMessage(content="not json"), "parsed": None, "parsing_error": ValueError("bad json")}
    llm, _ = make(FakeChat([broken]))
    with pytest.raises(ExternalServiceError):
        llm.complete_json("s", "u", Analysis)


# tags는 호출 설정(config)으로 그대로 넘어간다("nostream" 태그가 생성 호출의 출력 유출을 막는 데 쓰인다).
def test_gemini_llm_passes_tags_through():
    chat = FakeChat()
    make(chat)[0].complete_json("s", "u", Analysis, tags=["nostream"])
    assert chat.last_config["tags"] == ["nostream"]


# tags를 안 주면 빈 태그를 억지로 만들지 않는다(그래프가 물려준 설정을 덮어쓰지 않도록).
def test_no_tags_means_no_tags_in_config():
    chat = FakeChat()
    make(chat)[0].complete_json("s", "u", Analysis)
    assert not (chat.last_config or {}).get("tags")


# 토큰 사용량은 버리지 않고 남긴다(Langfuse 기록·한도 점검용).
def test_token_usage_is_kept():
    llm, _ = make(FakeChat())
    assert llm.last_usage is None
    llm.complete_json("s", "u", Analysis)
    assert llm.last_usage == USAGE


# 진짜 클라이언트를 만들 때 SDK 자체 재시도를 끈다(max_retries=1 = 한 번만 시도). 안 그러면 기본값 6회 재시도가
# 우리 재시도 위에 겹쳐 무료 한도를 더 태우고 요청이 오래 매달린다. 만들 때 네트워크는 쓰지 않는다.
def test_real_client_is_built_without_sdk_retries():
    client = GeminiLlm("fake-key", "gemini-3.5-flash-lite")._get_client()
    assert isinstance(client, ChatGoogleGenerativeAI) and client.max_retries == 1


# 가짜가 아니라 진짜 채팅 모델 클래스가 우리가 쓰는 호출 형태(스키마 + include_raw)를 받아들이는지 확인한다.
# 가짜 클라이언트만 쓰면 "가짜는 받는데 진짜는 거부하는" 어긋남을 놓친다. 만들 때 네트워크는 쓰지 않는다.
def test_real_client_accepts_our_structured_output_request():
    client = GeminiLlm("fake-key", "gemini-3.5-flash-lite")._get_client()
    assert client.with_structured_output(Analysis, include_raw=True) is not None


# 잘못된 재시도 횟수(0 이하)는 만드는 순간 거절한다: 한 번도 시도하지 않는 호출기가 되면 안 된다.
@pytest.mark.parametrize("bad", [0, -1])
def test_max_retries_must_be_positive(bad):
    with pytest.raises(ValueError):
        GeminiLlm("k", "m", max_retries=bad)
