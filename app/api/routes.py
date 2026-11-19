"""HTTP 엔드포인트: 로그인, 채팅(SSE), 피드백, 상태 확인.

역할: 화면(Task 15)이 부르는 API 전부. 요청을 검증하고, 계정별 사용 횟수를 세고, 그래프(run_chat)가 내는 이벤트를 SSE로 전달한다.
사용처: app/api/main.py가 `router`를 앱에 붙인다.

엔드포인트 계약(계획서 Task 14):
- POST /api/login {"username","password"} → 200 {"username","role"} + 세션 쿠키 / 401 / 429(시도가 너무 많음)
- POST /api/logout, GET /api/me → {"username","role"}
- POST /api/chat {"thread_id": UUID|null, "question"} → 200 text/event-stream / 401 / 422 / 429(`test`의 IP별 5회 초과)
- POST /api/feedback {"message_id", "value": 1|-1}
- GET /api/health → {"status": "ok", "opensearch": "green"|"yellow"|"red"|"down", "index_docs": int} (로그인 불필요)
HTTP 오류의 본문은 `{"detail": "화면에 그대로 보여 줄 문장"}`이다.

SSE 이벤트(채팅 응답 본문): `event: 이름\\ndata: JSON\\n\\n` 형식.
- progress {stage,label}: 단계가 시작됨. detail {stage,text}: 단계가 끝난 뒤의 한 줄 요약(찾은 조문 이름, 충분함/부족함).
- delta {part,index,text}: **검증을 통과한** 답변을 문장 단위로 흘려보냄. answer {Final.payload + message_id + thread_id}: 마지막 한 번.
- error {code,message}: 한도 초과(quota)나 서버 오류(internal). 이 경우 answer는 오지 않는다.
계획서의 계약에 없던 것: `answer`에 `thread_id`를 더했다(첫 질문에서 서버가 대화 번호를 만들어 주므로, 화면이 다음 질문에 그 번호를
다시 보내야 대화가 이어진다), `detail`·`delta`는 Task 13에서 사용자가 승인한 확장이다.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.adapters.sqlite_store import User
from app.api.auth import SESSION_COOKIE, SESSION_MAX_AGE_S, current_user, dummy_hash, verify_password
from app.api.limits import client_ip, normalize_ip
from app.domain.errors import QuotaExceeded
from app.graph.runner import AnswerDelta, Final, Progress, StageDetail, run_chat
from app.observability.langfuse_setup import Tracing
from app.texts import (
    INTERNAL_ERROR_MESSAGE,
    LOGIN_FAILED_MESSAGE,
    LOGIN_THROTTLED_MESSAGE,
    QUOTA_MESSAGE,
    invalid_question_message,
    limit_exceeded_message,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# 쿠키 속성은 설정할 때와 지울 때 같아야 브라우저가 같은 쿠키로 인식해 지운다.
_COOKIE_ATTRIBUTES = {"path": "/", "httponly": True, "secure": True, "samesite": "lax"}


class LoginBody(BaseModel):
    # 길이 상한: 터무니없이 긴 입력으로 bcrypt와 서버를 괴롭히지 못하게 한다.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class ChatBody(BaseModel):
    # UUID 타입이라 형식이 틀리면 422가 된다. 하이픈 없는 16진수·대문자도 받아서 한 가지 모양(소문자+하이픈)으로 통일한다.
    thread_id: UUID | None = None
    question: str


class FeedbackBody(BaseModel):
    message_id: str = Field(min_length=1, max_length=100)
    value: Literal[1, -1]


# ---- 로그인 ----


def _login_key(request: Request) -> str:
    """로그인 시도 제한에 쓰는 키: 요청자의 IP(사용 횟수와 같은 규칙으로 읽고 정규화한다)."""
    return normalize_ip(client_ip(request, request.app.state.settings.trusted_proxies))


@router.post("/login")
def login(body: LoginBody, request: Request) -> JSONResponse:
    """아이디·비밀번호를 확인하고 세션 쿠키를 준다.

    `def`(async 아님)로 둔 이유: bcrypt 확인이 0.2초쯤 걸리는 CPU 작업이라 async 함수 안에서 돌리면 그동안 서버 전체가 멈춘다.
    FastAPI는 일반 `def` 엔드포인트를 별도 스레드에서 실행하므로 다른 요청을 막지 않는다.
    """
    state = request.app.state
    key = _login_key(request)
    if state.login_throttle.is_blocked(key):
        raise HTTPException(status_code=429, detail=LOGIN_THROTTLED_MESSAGE)

    user = state.store.get_user(body.username)
    # 없는 계정이어도 같은 비용의 비교를 한 번 한다(응답 시간으로 계정 존재 여부가 새지 않게).
    password_ok = verify_password(body.password, user.password_hash if user else dummy_hash())
    if user is None or not password_ok:
        state.login_throttle.record_failure(key)
        raise HTTPException(status_code=401, detail=LOGIN_FAILED_MESSAGE)

    response = JSONResponse({"username": user.username, "role": user.role})
    response.set_cookie(SESSION_COOKIE, state.sessions.dumps(user.username), max_age=SESSION_MAX_AGE_S, **_COOKIE_ATTRIBUTES)
    return response


@router.post("/logout")
def logout() -> JSONResponse:
    """쿠키를 지운다. 세션을 서버에 저장하지 않으므로 서버 쪽에서 할 일은 없다."""
    response = JSONResponse({"status": "ok"})
    response.delete_cookie(SESSION_COOKIE, **_COOKIE_ATTRIBUTES)
    return response


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"username": user.username, "role": user.role}


# ---- 채팅 ----


def sse(name: str, data: dict) -> str:
    """SSE 이벤트 한 건을 문자열로 만든다. JSON은 줄바꿈을 `\\n`으로 이스케이프하므로 `data:` 한 줄에 들어간다."""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _frame(event: object, message_id: str, thread_id: str) -> str | None:
    """그래프 이벤트(runner.py)를 SSE 이벤트로 바꾼다. 모르는 종류는 보내지 않는다(내보내도 되는 것만 허용 목록으로 보낸다)."""
    if isinstance(event, Progress):
        return sse("progress", {"stage": event.stage, "label": event.label})
    if isinstance(event, StageDetail):
        return sse("detail", {"stage": event.stage, "text": event.text})
    if isinstance(event, AnswerDelta):
        return sse("delta", {"part": event.part, "index": event.index, "text": event.text})
    if isinstance(event, Final):
        return sse("answer", {**event.payload, "message_id": message_id, "thread_id": thread_id})
    return None


def _event_stream(
    graph: object,
    thread_id: str,
    question: str,
    message_id: str,
    refund: Callable[[], None],
    config_extra: dict | None = None,
) -> Iterator[str]:
    """그래프를 돌리며 SSE 이벤트를 하나씩 내보낸다. 실패하면 사용자용 문구만 담은 error 이벤트로 끝낸다.

    답이 나가지 못한 모든 경우(한도 초과, 외부 서비스·내부 오류)는 `refund()`로 사용 횟수를 돌려준다(스펙 6.6: 서버 오류는 차감하지
    않는다). 예외의 내용(외부 서비스 응답, 스택 트레이스)은 사용자에게 보내지 않고 서버 로그에만 남긴다.
    질문 원문은 로그에 쓰지 않는다(개인정보가 섞일 수 있다).
    config_extra: 관측(Langfuse) 콜백 등 그래프 호출에 더할 설정(`Tracing.run_config`가 만든 것).
    `except Exception`은 클라이언트가 연결을 끊을 때 생기는 GeneratorExit·취소(BaseException)를 잡지 않는다 — 그때는 환불 없이 끝난다.
    """
    try:
        for event in run_chat(graph, thread_id, question, config_extra):
            frame = _frame(event, message_id, thread_id)
            if frame is not None:
                yield frame
    except QuotaExceeded:
        logger.warning("외부 서비스 무료 한도 초과 (thread_id=%s)", thread_id)
        refund()
        yield sse("error", {"code": "quota", "message": QUOTA_MESSAGE})
    except Exception:
        logger.exception("채팅 처리 중 오류 (thread_id=%s)", thread_id)
        refund()
        yield sse("error", {"code": "internal", "message": INTERNAL_ERROR_MESSAGE})


@router.post("/chat")
def chat(body: ChatBody, request: Request, user: User = Depends(current_user)) -> StreamingResponse:
    """질문을 받아 SSE로 답한다.

    순서: ① 로그인 확인(의존성, 401) → ② 질문 검증(422, 차감 없음) → ③ `test` 계정이면 IP별 사용 횟수 소비(초과 시 429) → ④ 그래프 실행.
    ③을 검증 **뒤**에 두는 이유: 잘못된 질문으로 횟수를 잃지 않게 한다. 확인과 증가는 SQLite 한 트랜잭션이라 동시에 들어온 요청이
    5건을 넘겨 통과하지 못한다(Task 5).
    """
    state = request.app.state
    settings = state.settings

    question = body.question.strip()
    if not question or len(question) > settings.max_question_chars:
        raise HTTPException(status_code=422, detail=invalid_question_message(settings.max_question_chars))

    ip_key = normalize_ip(client_ip(request, settings.trusted_proxies))
    charged = False
    if user.role == "test":
        if not state.store.try_consume_ip_quota(ip_key, settings.test_ip_limit):
            raise HTTPException(status_code=429, detail=limit_exceeded_message(settings.test_ip_limit))
        charged = True

    def refund() -> None:
        if charged:
            state.store.refund_ip_quota(ip_key)

    thread_id = str(body.thread_id) if body.thread_id else str(uuid.uuid4())
    # 답변 하나를 가리키는 번호(message_id)는 화면이 받는 answer 이벤트와 Langfuse trace가 같은 것을 쓴다 — 그래야 👍/👎가
    # 그 답변의 trace를 찾아간다. 질문 검증과 사용 횟수 확인을 통과한 뒤에만 만든다(거절된 요청은 기록할 실행이 없다).
    message_id = str(uuid.uuid4())
    trace_config = state.tracing.run_config(message_id=message_id, thread_id=thread_id, username=user.username)
    stream = _event_stream(state.graph, thread_id, question, message_id, refund, trace_config)
    # Cache-Control: 중간 캐시가 응답을 모아 두지 않게. X-Accel-Buffering: Nginx가 이벤트를 모아서 한꺼번에 보내지 않게.
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---- 피드백, 상태 확인 ----


def log_feedback(username: str, message_id: str, value: int) -> None:
    """피드백을 로그에 남긴다(질문·답변 내용은 없고, 계정 이름과 답변 번호와 값만)."""
    logger.info("피드백 (user=%s, message_id=%s, value=%s)", username, message_id, value)


def make_feedback_handler(tracing: Tracing) -> Callable[[str, str, int], None]:
    """기본 피드백 처리기: 로그에 남기고 관측 연결에 점수로 넘긴다(`app.state.feedback_handler`에 끼운다).

    서버가 발급하지 않은 message_id는 관측 연결이 무시한다(점수를 마구 만들어 Langfuse 무료 한도를 태우는 일을 막는다).
    """

    def handler(username: str, message_id: str, value: int) -> None:
        log_feedback(username, message_id, value)
        tracing.submit_feedback(message_id, value)

    return handler


@router.post("/feedback")
def feedback(body: FeedbackBody, request: Request, user: User = Depends(current_user)) -> dict:
    """답변에 대한 👍(1)/👎(-1)를 받는다. 처리기는 요청마다 `app.state`에서 읽는다(나중에 바꿔 끼울 수 있도록)."""
    request.app.state.feedback_handler(user.username, body.message_id, body.value)
    return {"status": "ok"}


# 계약에 있는 OpenSearch 상태값. 이 밖의 값(unknown 등)은 "down"으로 접는다.
_OPENSEARCH_STATES = ("green", "yellow", "red")


@router.get("/health")
def health(request: Request) -> dict:
    """로그인 없이 부를 수 있는 상태 확인: `{"status", "opensearch": green|yellow|red|down, "index_docs"}`.

    `status`는 이 앱이 응답하고 있다는 뜻이라 늘 "ok"다. 검색엔진 상태는 따로 `opensearch`에 담는다 — "서버는 떠 있는데 검색엔진이
    죽었다"를 한 번의 호출로 구분하려는 것이다. 검색엔진이 응답하지 않아도 500이 아니라 `down`과 0으로 답한다.
    오류 메시지는 응답에 싣지 않는다(접속 정보가 섞여 있을 수 있고, 로그인 없이 누구나 부를 수 있는 주소다). 예외 종류만 로그에 남긴다.
    `def`(async 아님)인 이유: 검색엔진 호출이 막는(blocking) 호출이라 별도 스레드에서 돌려야 서버가 멈추지 않는다.
    """
    try:
        opensearch, index_docs = request.app.state.health_probe()
    except Exception as exc:  # 어떤 실패든 "down"으로 답한다(상태 확인이 스스로 죽으면 안 된다)
        logger.warning("OpenSearch 상태 확인 실패: %s", type(exc).__name__)
        opensearch, index_docs = "down", 0
    if opensearch not in _OPENSEARCH_STATES:
        opensearch = "down"
    return {"status": "ok", "opensearch": opensearch, "index_docs": index_docs}
