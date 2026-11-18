"""Langfuse Cloud 연결: 마스킹이 걸린 클라이언트, 질문마다 붙이는 콜백, 수동 기록 이벤트, 피드백 점수.

역할: 질문 한 턴이 Langfuse에서 trace 하나로 보이게 한다. 그래프 노드·LLM 호출은 LangChain 콜백(`CallbackHandler`)이 자동으로
기록하고, 질문 분류·리랭크 폴백·갈림길 결과 같은 우리가 정한 값은 `record_event`가 이벤트로 남긴다(스펙 6.7).
사용처: app/container.py(그래프에 sink 연결), app/api/main.py(앱 조립), app/api/routes.py(채팅·피드백).

핵심 개념 3가지:

1. **마스킹은 보내기 직전(내보내기 단계)에서 한다** — `mask_otel_spans`. Langfuse SDK 5.x는 span을 만든 뒤 Cloud로 내보내기 직전에
   속성들을 훅에 넘긴다. 입력·출력·메타데이터·이벤트 기록이 모두 span 속성이므로 한곳에서 가려진다. 문서가 새 설정에는 이쪽을
   권장하고, 구형 `mask`는 SDK API로 만든 데이터만 덮는다. LLM에 가는 입력은 이 길을 지나지 않으므로 원문 그대로다.
2. **message_id가 곧 trace id의 씨앗이다** — 답변 하나(message_id)의 trace id를 `Langfuse.create_trace_id(seed=message_id)`로
   계산한다. 같은 씨앗은 항상 같은 id가 되므로, 나중에 👍/👎가 오면 따로 저장해 둔 것 없이 그 trace를 찾아가 점수를 붙인다.
3. **관측은 서비스를 멈추게 하지 않는다** — Langfuse가 고장 나도 답변은 나간다(실패는 경고 로그만, 예외 종류만 적고 내용은 적지 않는다).
"""

import logging
import threading
from collections import OrderedDict
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, Optional, Protocol

from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from langfuse.types import MaskOtelSpansParams, MaskOtelSpansResult, OtelSpanPatch

from app.config import Settings
from app.observability.masking import mask_pii, mask_serialized

logger = logging.getLogger(__name__)

RUN_NAME = "labor-rag-chat"  # Langfuse 화면에서 trace 이름으로 보인다
FEEDBACK_SCORE_NAME = "user-feedback"
# 서버가 발급한 message_id를 기억하는 개수 상한. 넘으면 오래된 것부터 잊는다. 시작값이며 측정한 값이 아니다.
MAX_ISSUED_MESSAGES = 2000


# ---- 마스킹 훅 ----


def _mask_attribute(value: Any) -> Any:
    """span 속성 값 하나를 가린다. 문자열은 JSON이면 풀어서, 문자열 목록은 항목마다 가린다. 숫자·불리언은 그대로."""
    if isinstance(value, str):
        return mask_serialized(value)
    if isinstance(value, (list, tuple)) and any(isinstance(item, str) for item in value):
        return type(value)(mask_serialized(item) if isinstance(item, str) else item for item in value)
    return value


def mask_otel_spans(*, params: MaskOtelSpansParams) -> Optional[MaskOtelSpansResult]:
    """내보낼 span들의 모든 문자열 속성에서 개인정보를 가린다(SDK의 `mask_otel_spans` 훅).

    바뀌는 속성이 있는 span에만 패치를 만든다. 아무것도 바뀌지 않으면 None을 돌려줘 SDK가 배치를 그대로 보낸다.
    **실패에 닫힘**: 어떤 속성을 가리다가 예외가 나면 그 속성을 지워서 보낸다(가리지 못한 원문이 나가는 것보다 낫다).
    이 함수가 통째로 예외를 내면 SDK가 그 배치 전체를 버리므로, 속성 단위로 잡아서 span 하나의 문제가 배치 전체로 번지지 않게 한다.

    한계: span의 **이벤트**와 OTel **상태 설명**은 속성이 아니라서 이 훅이 못 고친다. 상태 설명(오류 문구)은 아래
    `_MaskedCallbackHandler`가 따로 가린다. 이름·주소처럼 패턴이 없는 개인정보는 가리지 못한다(masking.py 참고).
    """
    patches: dict = {}
    for identifier, span in params.spans.items():
        changes: dict[str, Any] = {}
        deletions: list[str] = []
        for key, value in span.attributes.items():
            try:
                masked = _mask_attribute(value)
            except Exception:
                deletions.append(key)
                continue
            if masked != value:
                changes[key] = masked
        if changes or deletions:
            patches[identifier] = OtelSpanPatch(set_attributes=changes, delete_attributes=tuple(deletions))
    return MaskOtelSpansResult(span_patches=MappingProxyType(patches)) if patches else None


class _MaskedCallbackHandler(CallbackHandler):
    """오류 문구까지 가리는 Langfuse LangChain 콜백.

    왜 필요한가: 노드가 예외를 내면 SDK가 그 메시지를 속성(`status_message`)과 OTel **상태 설명** 두 곳에 쓴다. 속성은 위의
    훅이 가리지만 상태 설명은 훅이 못 건드린다. 외부 서비스 오류 메시지에 사용자 입력이 섞여 나올 수 있어서(예: 응답 검증 오류가
    입력값을 되풀이한다), 오류 문구가 만들어지는 SDK의 한 지점을 가려서 두 곳 모두에 가려진 문구가 쓰이게 한다.
    이 메서드는 SDK의 비공개(밑줄) 메서드라서 SDK 버전을 올릴 때 조용히 무력화될 수 있다 — tests/test_langfuse_setup.py의
    `test_error_messages_are_masked_in_attribute_and_status`가 실제 span을 보고 이를 지킨다(requirements는 SDK 버전을 고정한다).
    """

    def _get_error_level_and_status_message(self, error: BaseException) -> tuple[Literal["DEFAULT", "ERROR"], str]:
        level, message = super()._get_error_level_and_status_message(error)
        return level, mask_pii(message)


# ---- 연결 ----


class Tracing(Protocol):
    """API·그래프가 쓰는 관측 연결의 모양. 진짜(LangfuseTracing)와 꺼진 상태(NullTracing), 테스트용 가짜가 이 모양을 따른다."""

    def run_config(self, *, message_id: str, thread_id: str, username: str) -> dict:
        """질문 한 턴에 붙일 그래프 호출 설정(콜백, 실행 이름, 세션·사용자). 추적을 안 하면 빈 dict."""
        ...

    def record_event(self, name: str, payload: dict) -> None:
        """수동 기록 이벤트 하나(EventSink 모양)."""
        ...

    def submit_feedback(self, message_id: str, value: int) -> bool:
        """답변에 대한 👍(1)/👎(-1)를 점수로 남긴다. 남겼으면 True."""
        ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class LangfuseTracing:
    """진짜 Langfuse 연결. `build_langfuse(settings)`가 만든다.

    client: 마스킹 훅이 걸린 Langfuse 클라이언트. public_key: 콜백이 같은 클라이언트를 찾는 열쇠.
    max_issued: 기억할 message_id 개수 상한(`run_config`가 발급을 기록하고 `submit_feedback`이 확인한다).
    """

    def __init__(self, client: Langfuse, public_key: str, *, max_issued: int = MAX_ISSUED_MESSAGES) -> None:
        self.client = client
        self.public_key = public_key
        self._max_issued = max_issued
        self._issued: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()  # 채팅 요청은 스레드 여러 개에서 들어온다

    def trace_id_for(self, message_id: str) -> str:
        """message_id로 trace id(32자리 16진수)를 계산한다. 같은 message_id는 항상 같은 값이다."""
        return Langfuse.create_trace_id(seed=message_id)

    def run_config(self, *, message_id: str, thread_id: str, username: str) -> dict:
        """질문 한 턴의 그래프 호출에 더할 설정을 만들고, 이 message_id를 "서버가 발급한 것"으로 기억한다.

        - 콜백은 질문마다 새로 만든다(실행 상태를 들고 있어서 공유하면 안 된다). trace id는 message_id에서 정해진다.
        - `langfuse_session_id`(대화 번호)로 같은 대화의 여러 턴이 Langfuse에서 한 세션으로 묶이고, `langfuse_user_id`는 계정 이름이다.
        - 만들기에 실패하면 경고만 남기고 빈 설정을 돌려준다(추적 없이도 답변은 나간다).
        """
        try:
            handler = _MaskedCallbackHandler(
                public_key=self.public_key, trace_context={"trace_id": self.trace_id_for(message_id)}
            )
        except Exception as error:
            logger.warning("Langfuse 콜백을 만들지 못했습니다 (%s)", type(error).__name__)
            return {}
        with self._lock:
            self._issued[message_id] = None
            self._issued.move_to_end(message_id)
            while len(self._issued) > self._max_issued:
                self._issued.popitem(last=False)
        return {
            "callbacks": [handler],
            "run_name": RUN_NAME,
            "metadata": {"langfuse_session_id": thread_id, "langfuse_user_id": username},
        }

    def record_event(self, name: str, payload: dict) -> None:
        """수동 기록을 Langfuse 이벤트로 남긴다. 그래프 노드 안에서 불리면 지금 실행 중인 span 밑에 붙는다."""
        self.client.create_event(name=name, input=payload)

    def submit_feedback(self, message_id: str, value: int) -> bool:
        """👍(1)/👎(-1)를 그 답변의 trace에 점수로 붙인다.

        **서버가 발급하지 않은 message_id는 무시한다**(False): 로그인한 누구나 아무 문자열이나 보낼 수 있어서, 막지 않으면 점수를
        마구 만들어 Langfuse 무료 한도(월 50,000 units)를 태울 수 있다. 같은 답변에 다시 보내면 같은 점수 번호(score_id)로
        덮어쓴다(마음을 바꿔도 점수가 늘어나지 않는다 — 덮어쓰기는 Langfuse 문서의 동작을 따른 가정이고 실측 전이다).
        """
        if value not in (1, -1):
            raise ValueError("피드백 값은 1(좋아요) 또는 -1(별로예요)이어야 합니다")
        with self._lock:
            if message_id not in self._issued:
                return False
        try:
            self.client.create_score(
                name=FEEDBACK_SCORE_NAME,
                value=value,
                trace_id=self.trace_id_for(message_id),
                score_id=f"{FEEDBACK_SCORE_NAME}-{message_id}",
                data_type="NUMERIC",
            )
        except Exception as error:
            logger.warning("피드백 점수를 남기지 못했습니다 (%s)", type(error).__name__)
            return False
        return True

    def flush(self) -> None:
        """모아 둔 기록을 지금 보낸다(시험, 서버 종료 직전)."""
        self.client.flush()

    def shutdown(self) -> None:
        """남은 기록을 보내고 연결을 닫는다."""
        self.client.shutdown()


class NullTracing:
    """추적이 꺼진 상태: 모든 호출이 아무 일도 하지 않는다. Langfuse 키 없이 시험·개발할 때 쓴다."""

    def run_config(self, *, message_id: str, thread_id: str, username: str) -> dict:
        return {}

    def record_event(self, name: str, payload: dict) -> None:
        return None

    def submit_feedback(self, message_id: str, value: int) -> bool:
        return False

    def flush(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


def build_langfuse(settings: Settings, *, span_exporter: Any = None) -> LangfuseTracing:
    """설정으로 Langfuse 연결을 만든다. 만들 때 네트워크를 쓰지 않는다(기록은 모아서 나중에 보낸다).

    span_exporter: 시험용. 메모리 exporter를 넘기면 Cloud 대신 거기에 쌓인다(없으면 Langfuse Cloud로 보낸다).
    같은 공개 키로 두 번 만들면 SDK가 처음 만든 연결을 재사용하므로, 운영에서는 앱 하나에 한 번만 만든다.
    """
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_host,
        mask_otel_spans=mask_otel_spans,
        span_exporter=span_exporter,
    )
    return LangfuseTracing(client, settings.langfuse_public_key)
