"""서비스 조립: 설정(Settings)으로 진짜 부품을 만들어 그래프에 끼운다.

역할: 이 프로젝트에서 **진짜 어댑터(Gemini·OpenSearch·Cohere)를 만드는 유일한 곳**이다. 그래프 노드는 포트(app/ports.py)만 알고,
테스트는 가짜 부품을 끼우며(tests/conftest.py의 deps_with), 운영은 여기서 진짜 부품을 끼운다.
사용처: app/api/main.py(서버), 평가 러너(Task 17, 서버와 똑같은 그래프로 평가하려고 같은 함수를 쓴다).

약속: 부품을 **만들 때는 네트워크를 쓰지 않는다**(각 어댑터가 처음 호출할 때 연결한다). 덕분에 설정이 틀려도 서버가 켜질 때 외부
서비스 상태에 좌우되지 않고, 이 파일의 시험이 네트워크 없이 돈다(tests/test_container.py가 소켓을 막고 확인한다).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph

from app.adapters.cohere_rerank import CohereReranker
from app.adapters.gemini import GeminiEmbedder, GeminiLlm
from app.adapters.opensearch_store import OpenSearchStore, make_client
from app.adapters.sqlite_store import AppStore
from app.config import Settings
from app.graph.build import build_graph
from app.graph.deps import GraphDeps
from app.graph.state import checkpoint_serde
from app.observability.langfuse_setup import Tracing
from app.retrieval.rerank_gate import RerankGate
from app.scope import COVERED_LAWS


def make_checkpointer(path: str) -> SqliteSaver:
    """대화 상태를 저장하는 SQLite 체크포인터를 만든다(폴더가 없으면 만든다).

    핵심 개념 — 체크포인터: LangGraph는 노드가 끝날 때마다 상태를 여기에 저장하고, 같은 `thread_id`로 다시 부르면 마지막
    상태에서 이어간다. 그래서 서버를 다시 시작해도 대화가 남는다.
    - `serde=checkpoint_serde()`가 꼭 필요하다: 없으면 저장된 `Hit`·`Chunk`가 dict로 복원되어 같은 대화의 다음 턴이
      `hit.chunk.text`에서 죽는다(Task 13에서 확인). 우리 도메인 타입만 허용 목록에 올린 직렬화기다.
    - `check_same_thread=False`: FastAPI가 요청을 여러 스레드에서 처리한다. SqliteSaver가 내부 잠금으로 연결 사용을 직렬화한다.
    - WAL 모드와 대기 시간(30초): 정리 스크립트(prune_checkpoints.py)가 같은 파일을 열어도 서로 막지 않는다.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    connection.execute("PRAGMA journal_mode=WAL")
    return SqliteSaver(connection, serde=checkpoint_serde())


def build_deps(settings: Settings, store: AppStore, tracing: Tracing | None = None) -> GraphDeps:
    """설정으로 진짜 부품 묶음(GraphDeps)을 만든다.

    store: 월 리랭크 호출 수를 세는 SQLite 저장소(앱이 쓰는 것과 같은 것을 받는다).
    tracing: 관측(Langfuse) 연결. 주면 그 `record_event`가 그래프의 수동 기록 sink가 된다(없으면 기록하지 않는다).
    검색 깊이(`search_depth`)는 포트(SearchPort.search)에 자리가 없어서 OpenSearchStore 생성자로 넘긴다.
    """
    client = make_client(settings.opensearch_url, settings.opensearch_user, settings.opensearch_password)
    return GraphDeps(
        llm=GeminiLlm(settings.gemini_api_key, settings.llm_model, timeout_s=settings.llm_timeout_s),
        # 웹 요청용 임베더: 재시도는 LLM과 같은 값(총 2회, 대기 최대 20초)으로 줄이고 제한 시간을 건다. 기본값(3회·60초)은 색인용이라
        # 그대로 쓰면 무료 한도가 소진된 순간 사용자가 임베딩 단계에서 최대 약 2분을 기다린다(최종 검토 지적 M-2).
        embedder=GeminiEmbedder(
            settings.gemini_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            max_retries=2,
            max_wait_s=20.0,
            timeout_s=settings.embedding_timeout_s,
        ),
        search=OpenSearchStore(client, settings.index_name, depth=settings.search_depth),
        rerank=RerankGate(
            CohereReranker(settings.cohere_api_key, timeout_s=settings.rerank_timeout_s), store, cap=settings.rerank_monthly_cap
        ),
        settings=settings,
        covered_laws=list(COVERED_LAWS),
        sink=tracing.record_event if tracing is not None else None,
    )


def build_health_probe(settings: Settings) -> Callable[[], tuple[str, int]]:
    """`/api/health`가 부르는 검색엔진 상태 확인 함수를 만든다: `() -> (상태, 색인 문서 수)`.

    만들 때 네트워크를 쓰지 않는다(위 약속). 부를 때 OpenSearch가 응답하지 않으면 예외가 올라가고, 엔드포인트가 "down"으로 바꾼다.
    그래프용 클라이언트와 따로 하나를 더 만든다: 상태 확인은 짧은 요청이라 연결을 공유할 이유가 없고, 서로의 실패에 영향받지 않는다.
    """
    client = make_client(settings.opensearch_url, settings.opensearch_user, settings.opensearch_password)
    return OpenSearchStore(client, settings.index_name).health


def build_graph_factory(
    settings: Settings, store: AppStore | None = None, tracing: Tracing | None = None
) -> Callable[[], CompiledStateGraph]:
    """그래프를 만들어 주는 함수를 돌려준다. 돌려받은 함수를 부르면 진짜 부품이 끼워진 컴파일된 그래프가 나온다.

    store를 주면 그것을 쓰고(앱이 이미 만든 저장소와 같은 것), 없으면 `settings.sqlite_path`로 새로 연다.
    tracing을 주면 그래프의 수동 기록이 그 연결로 나간다.
    """

    def factory() -> CompiledStateGraph:
        the_store = store if store is not None else AppStore(settings.sqlite_path)
        return build_graph(build_deps(settings, the_store, tracing), make_checkpointer(settings.checkpoint_path))

    return factory
