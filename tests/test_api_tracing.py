"""API가 관측(Langfuse) 연결을 질문·피드백에 이어 붙이는 방식을 시험한다 (Task 16).

진짜 Langfuse는 쓰지 않고, 호출 인자를 적어 두는 가짜 연결(SpyTracing)로 "무엇이 어디로 전달되는가"만 본다.
SDK가 실제로 만드는 span의 모양은 test_langfuse_setup.py가 본다.
"""

from __future__ import annotations

import uuid

from langchain_core.callbacks import BaseCallbackHandler

from app.observability.langfuse_setup import NullTracing
from tests.api_support import login_cookie, make_app, make_client, parse_sse, post_chat
from tests.conftest import ADMIN_PASSWORD, TEST_PASSWORD, FakeLlm


class ChainCounter(BaseCallbackHandler):
    """그래프가 실행될 때 불린 체인 이름을 적어 두는 콜백. 추가 설정(callbacks)이 실제 실행까지 닿았는지 본다."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, **kwargs):
        self.names.append(kwargs.get("name") or "")


class SpyTracing(NullTracing):
    """호출 인자를 적어 두는 가짜 관측 연결. run_config는 ChainCounter 콜백을 돌려준다."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.feedback: list[tuple[str, int]] = []
        self.counter = ChainCounter()

    def run_config(self, *, message_id: str, thread_id: str, username: str) -> dict:
        self.runs.append({"message_id": message_id, "thread_id": thread_id, "username": username})
        return {"callbacks": [self.counter], "run_name": "spy-run"}

    def submit_feedback(self, message_id: str, value: int) -> bool:
        self.feedback.append((message_id, value))
        return True


async def _admin(app) -> str:
    return await login_cookie(app, "admin", ADMIN_PASSWORD)


async def _tester(app) -> str:
    return await login_cookie(app, "test", TEST_PASSWORD)


def _answer(response) -> dict:
    answers = [data for name, data in parse_sse(response.text) if name == "answer"]
    assert len(answers) == 1, response.text
    return answers[0]


# 질문 한 건마다 관측 연결이 한 번 불리고, 넘긴 message_id·thread_id는 화면이 받는 answer 이벤트와 같다(피드백이 그 trace를 찾아간다).
async def test_chat_gives_the_tracing_the_same_ids_the_client_receives(tmp_path):
    spy = SpyTracing()
    app = make_app(tmp_path, tracing=spy)
    response = await post_chat(app, await _admin(app))
    answer = _answer(response)
    assert spy.runs == [{"message_id": answer["message_id"], "thread_id": answer["thread_id"], "username": "admin"}]


# 관측 연결이 돌려준 추가 설정(콜백, 실행 이름)이 그래프 실행에 실제로 전달된다.
async def test_tracing_config_reaches_the_graph_run(tmp_path):
    spy = SpyTracing()
    app = make_app(tmp_path, tracing=spy)
    await post_chat(app, await _admin(app))
    assert "spy-run" in spy.counter.names


# 검증에서 떨어진 질문(422)에는 관측 연결을 부르지 않는다(기록할 실행이 없다).
async def test_rejected_question_does_not_start_a_trace(tmp_path):
    spy = SpyTracing()
    app = make_app(tmp_path, tracing=spy)
    response = await post_chat(app, await _admin(app), "   ")
    assert response.status_code == 422
    assert spy.runs == []


# 기본 피드백 처리기는 관측 연결의 점수 기록으로 이어진다(Task 14의 로그만 남기던 자리를 대신한다).
async def test_default_feedback_handler_submits_the_score(tmp_path):
    spy = SpyTracing()
    app = make_app(tmp_path, tracing=spy)
    message_id = str(uuid.uuid4())
    async with make_client(app, await _tester(app)) as client:
        response = await client.post("/api/feedback", json={"message_id": message_id, "value": -1})
    assert response.status_code == 200
    assert spy.feedback == [(message_id, -1)]


# 관측 연결을 주지 않으면(시험·개발) 꺼진 연결이 들어가고, 앱은 평소처럼 동작한다.
async def test_default_tracing_is_off_when_a_graph_is_injected(tmp_path):
    app = make_app(tmp_path, llm=FakeLlm.script())
    assert isinstance(app.state.tracing, NullTracing)
    assert _answer(await post_chat(app, await _admin(app)))["refused"] is False
