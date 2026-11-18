"""리랭크 월 호출 한도 게이트: 비용 0원을 지키면서, 리랭크가 막혀도 답변은 계속 나가게 한다.

역할: Cohere 시험 키는 월 1,000회까지 무료다. 이 게이트가 호출 수를 직접 세다가 `cap`(기본 950)에 이르면
리랭커를 부르지 않고 **RRF 상위 결과를 그대로** 쓴다. Cohere가 한도·오류를 내도 똑같이 대신한다.
왜 950인가: 1,000회에서 50회를 남겨 두면, 아래의 "확인 뒤 증가" 사이에 동시 요청이 끼어드는 작은 어긋남과
달력 달 기준(아래 참고)의 오차를 흡수한다.
사용처: Task 10~13의 그래프가 `RerankStage`로 쓴다. 입력: 질문, RRF 후보 목록, top_n. 출력: RerankResult.

주의(확인하지 못한 것): Cohere가 월 한도를 **달력 달** 기준으로 초기화하는지는 문서로 확인하지 못했다.
여기서는 달력 달("YYYY-MM")로 센다. 결제 주기가 다르면 초기화 시점이 어긋날 수 있다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from app.adapters.sqlite_store import AppStore
from app.domain.errors import ExternalServiceError, QuotaExceeded
from app.domain.models import Hit, RerankResult
from app.ports import RerankerPort

logger = logging.getLogger(__name__)


class RerankGate:
    """`RerankStage` 구현: 한도 확인 → (허용되면) 리랭크 → 실패하면 RRF 순서로 폴백.

    cap: 한 달에 허용하는 호출 수(기본 950).
    today: 오늘 날짜를 돌려주는 함수. 테스트에서 달이 바뀌는 상황을 만들려고 바꿔 끼울 수 있게 했다.
    """

    def __init__(
        self,
        reranker: RerankerPort,
        store: AppStore,
        cap: int = 950,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._reranker = reranker
        self._store = store
        self._cap = cap
        self._today = today

    def rerank(self, query: str, hits: list[Hit], top_n: int = 5) -> RerankResult:
        """리랭크를 시도한다. 한도(`cap`)나 오류로 못 쓰면 `hits[:top_n]`(RRF 순서)과 그 이유를 돌려준다."""
        month = self._today().strftime("%Y-%m")

        # 한도에 닿았으면 리랭커를 부르지 않는다. 막힌 호출은 실제로 나간 요청이 아니므로 세지 않는다.
        if self._store.get_rerank_calls(month) >= self._cap:
            return RerankResult(hits[:top_n], False, "cap")

        # 요청을 보내기 **전에** 센다. 실패한 시도도 Cohere 쪽에서는 이미 한 번의 호출이라 한도를 쓴다.
        self._store.incr_rerank_calls(month)
        try:
            ranked = self._reranker.rerank(query, hits, top_n)
        except (QuotaExceeded, ExternalServiceError) as exc:
            logger.warning("리랭크 실패 — RRF 순서로 대신합니다(%s)", type(exc).__name__)
            return RerankResult(hits[:top_n], False, "error")
        return RerankResult(ranked, True, "ok")
