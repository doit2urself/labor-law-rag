"""채팅 API(SSE)와 피드백 API를 시험한다 (Task 14).

그래프는 진짜 LangGraph이고 LLM·검색·리랭커만 가짜다(tests/api_support.py). 그래서 "요청 → 그래프 → 이벤트 → 응답"이 이어서 시험된다.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.domain.errors import ExternalServiceError, QuotaExceeded
from app.texts import QUOTA_MESSAGE
from tests.api_support import PEER_IP, login_cookie, make_app, make_client, parse_sse, post_chat
from tests.conftest import ADMIN_PASSWORD, TEST_PASSWORD, FakeLlm


def answer_of(response) -> dict:
    """응답의 SSE 이벤트에서 `answer` 이벤트의 데이터를 꺼낸다(정확히 하나여야 한다)."""
    answers = [data for name, data in parse_sse(response.text) if name == "answer"]
    assert len(answers) == 1, response.text
    return answers[0]


def assert_ip_quota_fully_available(app, limit: int = 5) -> None:
    """이 IP의 사용 횟수가 0인지 확인한다: 한도만큼 연달아 소비해도 모두 성공해야 한다(차감이 남아 있으면 마지막이 실패)."""
    assert [app.state.store.try_consume_ip_quota(PEER_IP, limit) for _ in range(limit)] == [True] * limit


# ---- 접근 제어와 응답 형태 ----


# 로그인하지 않은 채 질문하면 401이고 안내 문구가 문장으로 온다.
async def test_chat_requires_login(api_app):
    response = await post_chat(api_app, None)
    assert response.status_code == 401
    assert isinstance(response.json()["detail"], str)


# SSE로 응답하고, 캐시·프록시 버퍼링이 이벤트를 붙잡지 않도록 헤더를 준다.
async def test_chat_streams_server_sent_events(api_app, admin_cookie):
    response = await post_chat(api_app, admin_cookie, "주휴수당이 뭐야?")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


# 이벤트 순서: 진행(progress)이 먼저 나오고, 마지막이 정확히 하나의 answer다. 질문 분석 단계가 맨 처음이다.
async def test_events_start_with_progress_and_end_with_one_answer(api_app, admin_cookie):
    events = parse_sse((await post_chat(api_app, admin_cookie)).text)
    names = [name for name, _ in events]
    assert names[0] == "progress" and events[0][1] == {"stage": "analyze", "label": "질문을 분석하는 중"}
    assert names[-1] == "answer" and names.count("answer") == 1
    assert set(names) <= {"progress", "detail", "delta", "answer"}


# 단계 요약(detail)과 글 조각(delta)도 중계된다. 조각을 이어 붙이면 answer의 문장과 같다.
async def test_detail_and_delta_events_are_relayed(api_app, admin_cookie):
    events = parse_sse((await post_chat(api_app, admin_cookie)).text)
    details = {data["stage"]: data["text"] for name, data in events if name == "detail"}
    assert details["sufficiency"] == "충분함" and details["search"].startswith("근로기준법 제51조")
    deltas = [data for name, data in events if name == "delta"]
    answer = events[-1][1]
    assert [d["text"] for d in deltas if d["part"] == "sentence"] == [s["text"] for s in answer["sentences"]]


# answer 데이터 = 그래프의 최종 값 + message_id(피드백용) + thread_id(다음 질문에서 대화를 이으려고).
async def test_answer_carries_payload_message_id_and_thread_id(api_app, admin_cookie):
    answer = answer_of(await post_chat(api_app, admin_cookie))
    assert {"refused", "preface", "sentences", "sources", "notice", "message_id", "thread_id"} <= set(answer)
    assert answer["refused"] is False and answer["sources"][0]["law_name"] == "근로기준법"
    uuid.UUID(answer["thread_id"])  # UUID 형식이다
    other = answer_of(await post_chat(api_app, admin_cookie))
    assert other["message_id"] != answer["message_id"] and other["thread_id"] != answer["thread_id"]


# 검증을 통과하지 못한 초안 문장이 어떤 이벤트에도 실리지 않는다(스펙 6.4의 핵심 안전 장치가 API까지 이어지는지).
async def test_unverified_draft_never_reaches_the_response(tmp_path):
    llm = FakeLlm.script(first_answer_with_text="DRAFT-SENTINEL-123", verification_fails_once=True)
    app = make_app(tmp_path, llm=llm)
    cookie = await login_cookie(app, "admin", ADMIN_PASSWORD)
    response = await post_chat(app, cookie)
    assert response.status_code == 200 and "DRAFT-SENTINEL-123" not in response.text
    assert llm.call_count > 3  # 실제로 재생성이 일어났다(시험이 의미 있으려면 필요하다)


# ---- 대화 이어가기(thread_id) ----


# 같은 thread_id로 보내면 앞선 대화가 질문 분석에 전달되고, 새 대화(thread_id 없음)는 전달되지 않는다.
async def test_thread_id_continues_the_conversation(tmp_path):
    llm = FakeLlm.script()
    app = make_app(tmp_path, llm=llm)
    cookie = await login_cookie(app, "admin", ADMIN_PASSWORD)
    first = answer_of(await post_chat(app, cookie, "주휴수당이 뭐야?"))
    second = answer_of(await post_chat(app, cookie, "그럼 알바는요?", thread_id=first["thread_id"]))
    fresh = answer_of(await post_chat(app, cookie, "퇴직금은요?"))
    analysis = [p.user for p in llm.prompts if p.schema.__name__ == "Analysis"]
    assert second["thread_id"] == first["thread_id"]
    assert "주휴수당이 뭐야?" in analysis[1]
    assert "주휴수당이 뭐야?" not in analysis[2] and fresh["thread_id"] != first["thread_id"]


# 하이픈 없는 16진수·대문자로 써도 같은 UUID이므로 같은 대화로 이어진다(대화 키가 한 가지 모양으로만 저장된다).
async def test_thread_id_is_canonicalised(api_app, admin_cookie):
    first = answer_of(await post_chat(api_app, admin_cookie))
    spelled_differently = first["thread_id"].replace("-", "").upper()
    again = answer_of(await post_chat(api_app, admin_cookie, thread_id=spelled_differently))
    assert again["thread_id"] == first["thread_id"]


# UUID가 아닌 thread_id는 422이고 사용 횟수를 차감하지 않는다.
async def test_non_uuid_thread_id_is_rejected_and_not_counted(api_app, test_cookie):
    response = await post_chat(api_app, test_cookie, thread_id="not-a-uuid")
    assert response.status_code == 422 and isinstance(response.json()["detail"], str)
    assert_ip_quota_fully_available(api_app)


# 필수 필드가 빠진 질문 요청은 422이고 detail이 화면에 보일 문장이다.
async def test_malformed_chat_body_gives_422_with_text_detail(api_app, admin_cookie):
    async with make_client(api_app, admin_cookie) as client:
        response = await client.post("/api/chat", json={"nothing": 1})
    assert response.status_code == 422 and isinstance(response.json()["detail"], str)


# ---- 오류 처리: 친절한 문구, 스택 트레이스 없음, 사용 횟수 환불 ----


# Gemini 한도 초과: 안내 문구가 error 이벤트로 나가고, test 계정의 사용 횟수는 돌려받는다.
async def test_gemini_quota_error_gives_friendly_message_and_refunds(tmp_path):
    app = make_app(tmp_path, llm=FakeLlm([QuotaExceeded("429 RESOURCE_EXHAUSTED secret-detail")]))
    cookie = await login_cookie(app, "test", TEST_PASSWORD)
    response = await post_chat(app, cookie)
    assert QUOTA_MESSAGE in response.text and "Traceback" not in response.text
    assert "secret-detail" not in response.text
    errors = [data for name, data in parse_sse(response.text) if name == "error"]
    assert errors == [{"code": "quota", "message": QUOTA_MESSAGE}]
    assert_ip_quota_fully_available(app)


# 외부 서비스 오류(시간 초과 등): 일반 안내 문구만 보이고 상세 내용은 새지 않으며 횟수는 환불된다.
async def test_external_service_error_is_internal_and_refunds(tmp_path):
    app = make_app(tmp_path, llm=FakeLlm([ExternalServiceError("503 upstream-secret-detail")]))
    cookie = await login_cookie(app, "test", TEST_PASSWORD)
    response = await post_chat(app, cookie)
    errors = [data for name, data in parse_sse(response.text) if name == "error"]
    assert [e["code"] for e in errors] == ["internal"]
    assert "upstream-secret-detail" not in response.text and "Traceback" not in response.text
    assert_ip_quota_fully_available(app)


# 예상하지 못한 예외: 화면에는 일반 문구만, 서버 로그에는 원인이 남고, 횟수는 환불된다.
async def test_unexpected_error_is_hidden_from_the_user_but_logged(tmp_path, caplog):
    app = make_app(tmp_path, llm=FakeLlm([RuntimeError("boom-sentinel")]))
    cookie = await login_cookie(app, "test", TEST_PASSWORD)
    with caplog.at_level(logging.INFO):
        response = await post_chat(app, cookie, "내 전화번호는 010-QUESTION-SENTINEL 입니다")
    assert [data["code"] for name, data in parse_sse(response.text) if name == "error"] == ["internal"]
    assert "boom-sentinel" not in response.text and "Traceback" not in response.text
    assert "boom-sentinel" in caplog.text  # 개발자는 서버 로그에서 원인을 볼 수 있다
    assert "QUESTION-SENTINEL" not in caplog.text  # 질문 원문(개인정보가 섞일 수 있음)은 로그에 남기지 않는다
    assert_ip_quota_fully_available(app)


# 서버 오류가 난 응답에는 answer 이벤트가 없다(반쯤 만든 답이 나가지 않는다).
async def test_error_response_has_no_answer_event(tmp_path):
    app = make_app(tmp_path, llm=FakeLlm([QuotaExceeded()]))
    cookie = await login_cookie(app, "admin", ADMIN_PASSWORD)
    names = [name for name, _ in parse_sse((await post_chat(app, cookie)).text)]
    assert "answer" not in names and names[-1] == "error"


# ---- 피드백 ----


# 로그인하지 않은 피드백 요청은 401이다.
async def test_feedback_requires_login(api_app):
    async with make_client(api_app) as client:
        response = await client.post("/api/feedback", json={"message_id": "m1", "value": 1})
    assert response.status_code == 401


# 로그인한 사용자의 👍/👎는 피드백 처리기로 전달된다(Task 16이 이 자리에 Langfuse 점수 기록을 끼운다).
async def test_feedback_is_passed_to_the_handler(api_app, test_cookie):
    seen: list[tuple] = []
    api_app.state.feedback_handler = lambda username, message_id, value: seen.append((username, message_id, value))
    async with make_client(api_app, test_cookie) as client:
        response = await client.post("/api/feedback", json={"message_id": "m1", "value": -1})
    assert (response.status_code, response.json()) == (200, {"status": "ok"})
    assert seen == [("test", "m1", -1)]


# 값이 1/-1이 아니거나 message_id가 비었거나 빠진 피드백은 422이고 처리기를 부르지 않는다.
@pytest.mark.parametrize(
    "body",
    [
        {"message_id": "m1", "value": 0},
        {"message_id": "m1", "value": 2},
        {"message_id": "", "value": 1},
        {"value": 1},
    ],
)
async def test_invalid_feedback_is_rejected(api_app, test_cookie, body):
    seen: list[tuple] = []
    api_app.state.feedback_handler = lambda *args: seen.append(args)
    async with make_client(api_app, test_cookie) as client:
        response = await client.post("/api/feedback", json=body)
    assert response.status_code == 422 and seen == []


# ---- 화면에 나가는 이벤트는 허용 목록만 ----


class LeakyGraph:
    """그래프가 (실수로) 허용 목록에 없는 값을 흘려보내는 상황을 흉내 낸다: 초안 같은 dict와 모르는 객체."""

    def stream(self, *args, **kwargs):
        from app.graph.runner import Final, Progress

        yield Progress("analyze", "질문을 분석하는 중")
        yield {"draft": "DRAFT-SENTINEL-456"}
        yield object()
        yield Final({"refused": False, "preface": None, "sentences": [], "sources": [], "notice": []})


# API는 Progress·StageDetail·AnswerDelta·Final 네 종류만 내보내고 나머지는 버린다. 그래프 쪽에 구멍이 생겨도 초안이 화면까지 가지 않는다.
async def test_events_outside_the_allow_list_are_dropped(tmp_path):
    app = make_app(tmp_path, graph_factory=LeakyGraph)
    cookie = await login_cookie(app, "admin", ADMIN_PASSWORD)
    response = await post_chat(app, cookie)
    assert "DRAFT-SENTINEL-456" not in response.text
    assert [name for name, _ in parse_sse(response.text)] == ["progress", "answer"]
