"""그래프 노드가 쓰는 외부 부품(의존성) 묶음.

역할: 노드는 Gemini·OpenSearch·Cohere를 직접 부르지 않고, 여기 담긴 포트(app/ports.py의 약속)만 쓴다.
노드를 만드는 함수에 `deps`를 한 번 넘기면(예: `analyze_node(deps)`) 노드가 그 부품들을 붙잡고 있다가 쓴다.
사용처: 모든 노드 팩토리(nodes_*.py), 그래프 조립(build.py), 서비스 조립(container.py), 테스트(tests/conftest.py의 deps_with).

테스트에서는 가짜 구현체를 끼우고, 운영에서는 진짜 어댑터를 끼운다. 이렇게 "만드는 쪽"에서 부품을 정해 주는 방식을
의존성 주입이라고 한다. 덕분에 그래프 로직은 어떤 공급자를 쓰는지 모른 채로 시험할 수 있다.
"""

from dataclasses import dataclass

from app.config import Settings
from app.ports import EmbedderPort, EventSink, LlmPort, RerankStage, SearchPort


@dataclass(frozen=True)
class GraphDeps:
    """그래프 한 벌이 쓰는 부품 모음. frozen이라 만든 뒤 몰래 바뀌지 않는다.

    covered_laws: 수집·색인한 법령 이름 목록. 질문 분석이 "근로 분야지만 수집 범위 밖"을 가려내는 기준이다.
    sink: 수동 기록 이벤트를 받는 곳(Langfuse 등). 없으면(None) 기록하지 않는다. 그래프가 마스킹한 값만 넘긴다.
    """

    llm: LlmPort
    embedder: EmbedderPort
    search: SearchPort
    rerank: RerankStage
    settings: Settings
    covered_laws: list[str]
    sink: EventSink | None = None
