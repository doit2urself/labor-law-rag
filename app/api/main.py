"""FastAPI 앱 만들기.

역할: 설정을 읽고, 저장소·계정·그래프를 준비하고, 라우터를 붙여 실행 가능한 앱을 돌려준다.
사용처: 서버 실행(`uvicorn app.api.main:create_app --factory --workers 1`, Task 18의 systemd 유닛)과 API 테스트.

핵심 설계:
- **모든 준비를 `create_app` 안에서 바로 한다**(서버가 켜질 때). 잘못된 설정(해시 형식 오류, 필수 값 누락)이 첫 요청이 아니라
  시작할 때 드러나게 하려는 것이다. 조용히 틀린 채 도는 것보다 시작이 실패하는 쪽이 낫다.
- **uvicorn 워커는 반드시 1개**: 로그인 시도 기록이 프로세스 메모리에 있고 SQLite 체크포인터를 한 프로세스가 쓴다(스펙 6.1).
- 오류 응답에는 스택 트레이스·내부 값이 없다. 처리하지 못한 예외는 uvicorn이 본문 없는 500으로 돌려주고 원인은 서버 로그에만 남는다.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langgraph.graph.state import CompiledStateGraph

from app.adapters.sqlite_store import AppStore
from app.api.auth import SessionCodec, looks_like_bcrypt
from app.api.limits import LoginThrottle, validate_trusted_proxies
from app.api.routes import make_feedback_handler, router
from app.config import Settings
from app.container import build_graph_factory
from app.observability.langfuse_setup import NullTracing, Tracing, build_langfuse
from app.texts import INVALID_REQUEST_MESSAGE


def _check_settings(settings: Settings) -> None:
    """시작할 때 설정 값을 확인한다. 해시 자리에 비밀번호 원문 같은 것이 들어 있으면 멈춘다.

    오류 메시지에는 변수 **이름**만 쓰고 값은 쓰지 않는다(값이 비밀번호 원문일 수 있어서 로그에 남기면 안 된다).
    """
    for name, value in (
        ("LABOR_ADMIN_PASSWORD_HASH", settings.admin_password_hash),
        ("LABOR_TEST_PASSWORD_HASH", settings.test_password_hash),
    ):
        if not looks_like_bcrypt(value):
            raise ValueError(f"{name}가 bcrypt 해시 형식이 아닙니다. scripts/make_password_hash.py로 만든 값을 넣으세요.")
    validate_trusted_proxies(settings.trusted_proxies)


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """형식이 틀린 요청을 422로 돌려준다. 기본 응답은 오류 목록(JSON 배열)이라 화면이 그대로 보여 줄 수 없어서 문장으로 바꾼다.

    문제가 된 **필드 이름**만 알려 주고 사용자가 보낸 값은 되돌려 보내지 않는다(개인정보가 섞여 있을 수 있다).
    """
    fields = sorted({str(error["loc"][-1]) for error in exc.errors() if error.get("loc")})
    detail = f"{INVALID_REQUEST_MESSAGE} ({', '.join(fields)})" if fields else INVALID_REQUEST_MESSAGE
    return JSONResponse(status_code=422, content={"detail": detail})


def create_app(
    settings: Settings | None = None,
    graph_factory: Callable[[], CompiledStateGraph] | None = None,
    tracing: Tracing | None = None,
) -> FastAPI:
    """앱을 만든다.

    settings: 없으면 환경 변수/`.env`에서 읽는다(`uvicorn --factory`는 인자 없이 부르므로). 필수 값이 빠지면 여기서 실패한다.
    graph_factory: 그래프를 만드는 함수. 없으면 진짜 부품을 끼운 그래프(container.py)를 쓴다. 테스트는 가짜 부품 그래프를 넘긴다.
    tracing: 관측(Langfuse) 연결. 없으면 **그래프도 따로 주지 않은 운영 경로**에서만 진짜 Langfuse 연결을 만든다. 시험처럼 그래프를
        직접 넘긴 경우에는 꺼진 연결(NullTracing)을 끼운다 — 가짜 키로 진짜 Langfuse 클라이언트를 만들어 두지 않기 위해서다.
    """
    if settings is None:
        settings = Settings()
    _check_settings(settings)

    store = AppStore(settings.sqlite_path)
    # `.env`의 해시를 DB에 맞춘다. 비밀번호를 바꾸려면 .env의 해시를 바꾸고 서버를 다시 시작하면 된다.
    store.upsert_user("admin", settings.admin_password_hash, "admin")
    store.upsert_user("test", settings.test_password_hash, "test")

    if tracing is None:
        tracing = build_langfuse(settings) if graph_factory is None else NullTracing()
    factory = graph_factory if graph_factory is not None else build_graph_factory(settings, store, tracing)

    app = FastAPI(title="근로·노동 상담 RAG")
    app.state.settings = settings
    app.state.store = store
    app.state.graph = factory()
    app.state.sessions = SessionCodec(settings.session_secret)
    app.state.login_throttle = LoginThrottle()
    app.state.tracing = tracing
    app.state.feedback_handler = make_feedback_handler(tracing)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.include_router(router)
    return app
