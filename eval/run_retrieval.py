"""검색 품질 측정: 평가셋의 질문으로 실제 검색·리랭크를 돌려 Recall@k, MRR, 리랭크 폴백 비율을 낸다 (Task 9).

사용법(프로젝트 루트에서):
    python -m eval.run_retrieval                                  # 깊이 100·150·200을 비교(리랭크 없음, 무료 한도 거의 안 씀)
    python -m eval.run_retrieval --rerank                         # 위 + 가장 나은 깊이에서 리랭크 측정(Cohere 호출 사용)
    python -m eval.run_retrieval --depths 150 --field casual_question --rerank --out data/retrieval_eval.json

서버의 OpenSearch는 127.0.0.1에만 열려 있으므로 이 PC에서 돌릴 때는 SSH 터널을 열고
`LABOR_OPENSEARCH_URL=https://127.0.0.1:19200`을 지정한다(deploy/opensearch-notes.md 6절). 키는 `.env`에서 읽는다
(LABOR_GEMINI_API_KEY, LABOR_OPENSEARCH_PASSWORD, 리랭크를 쓰면 LABOR_COHERE_API_KEY).

무엇을 재나
- **질문을 그대로 검색에 쓴다**(LLM 질문 재작성 없음). 서비스는 LLM이 질문을 검색어로 다시 쓰지만, 그러면 측정마다 LLM 호출이
  들고 결과가 흔들린다. 그래서 여기서는 "검색기 자체의 품질"만 잰다. 실제 서비스의 종단 결과는 Task 17이 잰다.
- Recall@k = 정답 조문 중 검색 상위 k개에 든 비율, MRR = 처음 맞힌 정답 순위의 역수(eval/metrics.py).
- 같은 질문을 `question`(조문 어휘형)과 `casual_question`(일상어형) 두 가지로 따로 잰다. 두 점수의 차이가 "조문에서 질문을 만들면
  점수가 부풀려진다"는 한계가 얼마나 큰지를 보여 준다(eval/evalset.py 설명).
- 깊이 비교: 하위 쿼리 깊이(`search_depth`)를 바꿔 같은 평가셋을 다시 돌린다. 리랭크 없이 RRF 순서만 보므로 임베딩(캐시됨)만 쓴다.
- 리랭크 측정: 정답이 없는 질문(거절 대상)도 리랭크해 점수 분포를 본다. "충분함/부족함"을 가르는 임계값(`rerank_min_score`) 후보를
  만드는 재료다.

무료 한도를 아끼는 장치
- 임베딩 캐시(질문 글 → 벡터)와 리랭크 캐시((질문, 후보 목록) → 결과)를 파일에 즉시 저장한다. 도중에 멈춰도 다시 실행하면 이어서 한다.
- 리랭크는 Cohere 시험 키가 분당 10회라서 진짜 호출 사이에 6.5초 간격을 둔다(너무 빠르면 429가 나서 폴백 비율이 부풀려진다).
- 리랭크는 한도(cap)·오류로 폴백한 결과를 캐시에 저장하지 않는다. 다시 실행하면 진짜 호출을 다시 시도한다.

종료 코드: 0 성공, 1 설정·입력 문제, 2 무료 한도 초과로 중단(캐시가 남으므로 다시 실행하면 이어서 한다).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import statistics
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# `python -m eval.run_retrieval`은 프로젝트 루트가 이미 경로에 있다. 직접 실행(`python eval/run_retrieval.py`)도 되게 넣어 둔다.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.domain.errors import QuotaExceeded  # noqa: E402
from app.domain.models import GoldKey, Hit, RerankResult  # noqa: E402
from app.ports import EmbedderPort, RerankStage  # noqa: E402
from eval.evalset import EvalItem, load_evalset  # noqa: E402
from eval.metrics import hit_keys, mrr, recall_at_k  # noqa: E402

FIELDS = ("question", "casual_question")
FIELD_NAMES = {"question": "조문 어휘형", "casual_question": "일상어형"}
DEFAULT_DEPTHS = (100, 150, 200)  # 스펙 6.3: 하위 쿼리 깊이 100~200
TOP_N = 5  # 리랭크 뒤 남기는 문서 수(Global Constraints: 후보 20 → 5)
COHERE_MIN_INTERVAL_S = 6.5  # 분당 10회 한도(10 req/min)보다 조금 여유를 둔 간격


# --- 기록·요약 모델 --------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """질문 하나를 검색한 결과 기록.

    ranked: 검색(RRF) 순서를 (법령명, 조문번호)로 바꾼 목록(한 조문은 한 번만).
    reranked: 리랭크를 했으면 그 뒤 상위 N개의 키, 안 했으면 None.
    top_score: 리랭커를 **실제로 썼을 때만** 1위 점수(폴백이면 None — 그때 점수는 RRF 점수라 의미가 다르다).
    rerank_reason: "ok" | "cap" | "error", 리랭크를 시도하지 않았으면 None.
    """

    item_id: str
    label: str
    field: str
    gold: frozenset[GoldKey]
    ranked: tuple[GoldKey, ...]
    reranked: tuple[GoldKey, ...] | None
    top_score: float | None
    rerank_reason: str | None


@dataclass(frozen=True)
class RerankSummary:
    """리랭크 단계 요약.

    tried: 리랭크를 시도한 질문 수. fallback_rate: 그중 리랭커를 못 쓰고 RRF 순서로 대신한 비율.
    scores_hit: 정답이 리랭크 상위 N에 든 질문의 1위 점수 / scores_miss: 정답이 있는데 빠진 질문 / scores_neg: 정답 없는 질문.
    """

    tried: int
    recall: float | None
    mrr: float | None
    fallback_rate: float
    scores_hit: tuple[float, ...]
    scores_miss: tuple[float, ...]
    scores_neg: tuple[float, ...]


@dataclass(frozen=True)
class Summary:
    """한 문장 종류(필드)의 요약. n은 정답이 있는 질문 수이며 Recall·MRR의 분모다."""

    field: str
    n: int
    recall: float
    mrr: float
    rerank: RerankSummary | None


def _mean(values: Sequence[float]) -> float:
    """평균. 비었으면 0.0(정답 있는 질문이 하나도 없으면 지표를 0으로 보이게 한다 — 표의 n 칸이 0이라 구분된다)."""
    return statistics.fmean(values) if values else 0.0


# --- 평가 루프 -------------------------------------------------------------------


def evaluate_retrieval(
    items: Sequence[EvalItem],
    *,
    field: str,
    search: Callable[[str], list[Hit]],
    rerank: RerankStage | None,
    top_n: int = TOP_N,
) -> list[Record]:
    """항목마다 선택한 필드의 문장으로 검색(과 리랭크)을 해 기록을 만든다.

    search: 질문 글 → 검색 결과(Hit 목록). 임베딩과 OpenSearch 호출은 이 함수 안에 숨어 있어서, 이 루프는 가짜로도 시험된다.
    해당 필드의 문장이 없는 항목(예: 검수에서 반려된 일상어 질문)은 건너뛴다 — 다른 문장으로 대신하면 두 필드의 점수가 섞인다.
    """
    records: list[Record] = []
    for item in items:
        text = getattr(item, field)
        if not text:
            continue
        hits = search(text)
        reranked: tuple[GoldKey, ...] | None = None
        top_score: float | None = None
        reason: str | None = None
        if rerank is not None:
            result = rerank.rerank(text, hits, top_n)
            reranked = tuple(hit_keys(result.hits))
            reason = result.reason
            if result.used_reranker and result.hits:
                top_score = result.hits[0].score
        records.append(Record(item.id, item.label, field, item.gold, tuple(hit_keys(hits)), reranked, top_score, reason))
    return records


def summarize(records: Sequence[Record], *, field: str, k: int, top_n: int) -> Summary:
    """한 필드의 기록을 요약한다. Recall·MRR은 **정답이 있는 질문만** 평균에 넣는다(정답 없는 거절 질문은 지표 대상이 아니다)."""
    mine = [r for r in records if r.field == field]
    scored = [r for r in mine if r.gold]
    recall = _mean([recall_at_k(r.gold, list(r.ranked), k) for r in scored])
    mean_mrr = _mean([mrr(r.gold, list(r.ranked)) for r in scored])

    tried = [r for r in mine if r.rerank_reason is not None]
    rerank_summary: RerankSummary | None = None
    if tried:
        tried_scored = [r for r in tried if r.gold and r.reranked is not None]
        hit_scores: list[float] = []
        miss_scores: list[float] = []
        neg_scores: list[float] = []
        for r in tried:
            if r.top_score is None:
                continue
            if not r.gold:
                neg_scores.append(r.top_score)
            elif r.reranked is not None and r.gold & set(r.reranked):
                hit_scores.append(r.top_score)
            else:
                miss_scores.append(r.top_score)
        rerank_summary = RerankSummary(
            tried=len(tried),
            recall=_mean([recall_at_k(r.gold, list(r.reranked), top_n) for r in tried_scored]) if tried_scored else None,
            mrr=_mean([mrr(r.gold, list(r.reranked)) for r in tried_scored]) if tried_scored else None,
            fallback_rate=sum(r.rerank_reason != "ok" for r in tried) / len(tried),
            scores_hit=tuple(hit_scores),
            scores_miss=tuple(miss_scores),
            scores_neg=tuple(neg_scores),
        )
    return Summary(field=field, n=len(scored), recall=recall, mrr=mean_mrr, rerank=rerank_summary)


def suggest_threshold(positive: Sequence[float], negative: Sequence[float]) -> tuple[float, float, float] | None:
    """충분성 임계값(`rerank_min_score`) 후보를 고른다. 돌려주는 값: (임계값, 진양성률, 진음성률).

    positive: 답할 수 있는 질문의 1위 리랭크 점수(임계값 이상이어야 "충분"). negative: 못 답하는 질문의 점수(미만이어야 "부족").
    후보는 이웃한 점수의 중간값들이고, (진양성률 + 진음성률 - 1)이 가장 큰 것을 고른다. 같으면 진양성률이 높은 쪽 —
    답할 수 있는 질문을 잘못 거절하는 것이 거절해야 할 질문에 답하는 것보다 더 나쁘다고 본 제 판단이다(명시된 요구가 아님).
    어느 한쪽이 비면 후보를 만들 수 없어 None.
    """
    if not positive or not negative:
        return None
    values = sorted(set(positive) | set(negative))
    candidates = [(a + b) / 2 for a, b in zip(values, values[1:])] or [values[0]]

    def rates(t: float) -> tuple[float, float]:
        return (sum(p >= t for p in positive) / len(positive), sum(n < t for n in negative) / len(negative))

    scored = [(t, *rates(t)) for t in candidates]
    return min(scored, key=lambda s: (-(s[1] + s[2] - 1), -s[1], s[0]))


def best_depth(by_depth: Mapping[int, Sequence[Summary]]) -> int:
    """깊이별 요약에서 가장 나은 깊이를 고른다: 필드 평균 Recall이 높은 쪽 → 같으면 평균 MRR → 같으면 작은 깊이."""
    return min(
        by_depth,
        key=lambda d: (-_mean([s.recall for s in by_depth[d]]), -_mean([s.mrr for s in by_depth[d]]), d),
    )


# --- 보고서 ----------------------------------------------------------------------


def _median(values: Sequence[float]) -> str:
    """점수 목록의 중앙값을 소수 둘째 자리 글로 만든다. 비었으면 "-"."""
    return f"{statistics.median(values):.2f}" if values else "-"


def format_report(
    by_depth: Mapping[int, Sequence[Summary]],
    *,
    rerank_summaries: Sequence[Summary] | None,
    k: int,
    top_n: int,
) -> str:
    """측정 결과를 마크다운 표로 만든다(docs/step0-report.md에 그대로 붙일 수 있다)."""
    lines = [
        f"### 검색(RRF 순서) — 깊이별 Recall@{k}·MRR",
        "",
        f"| 깊이 | 문장 | 정답 있는 질문 수 | Recall@{k} | MRR |",
        "|---|---|---|---|---|",
    ]
    for depth in sorted(by_depth):
        for s in by_depth[depth]:
            lines.append(f"| {depth} | {FIELD_NAMES[s.field]} | {s.n} | {s.recall:.2f} | {s.mrr:.2f} |")

    if rerank_summaries:
        lines += [
            "",
            f"### 리랭크 뒤(상위 {top_n}) — Recall@{top_n}·MRR·폴백 비율",
            "",
            f"| 문장 | 리랭크 시도 | Recall@{top_n} | MRR | 폴백 비율 |",
            "|---|---|---|---|---|",
        ]
        for s in rerank_summaries:
            r = s.rerank
            if r is None:
                continue
            recall = f"{r.recall:.2f}" if r.recall is not None else "-"
            mean_mrr = f"{r.mrr:.2f}" if r.mrr is not None else "-"
            lines.append(f"| {FIELD_NAMES[s.field]} | {r.tried} | {recall} | {mean_mrr} | {r.fallback_rate:.0%} |")

        lines += ["", "### 리랭크 1위 점수 분포(충분성 임계값 후보의 재료)", "",
                  "| 문장 | 정답이 상위에 든 질문(n·중앙값) | 정답이 빠진 질문(n·중앙값) | 정답 없는 질문(n·중앙값) | 임계값 후보 |",
                  "|---|---|---|---|---|"]
        for s in rerank_summaries:
            r = s.rerank
            if r is None:
                continue
            candidate = suggest_threshold(r.scores_hit, [*r.scores_miss, *r.scores_neg])
            text = (f"{candidate[0]:.2f} (답할 수 있는 질문 {candidate[1]:.0%} 통과, 못 답할 질문 {candidate[2]:.0%} 걸러짐)"
                    if candidate else "표본 부족")
            lines.append(
                f"| {FIELD_NAMES[s.field]} | {len(r.scores_hit)}·{_median(r.scores_hit)} | "
                f"{len(r.scores_miss)}·{_median(r.scores_miss)} | {len(r.scores_neg)}·{_median(r.scores_neg)} | {text} |"
            )
        lines += ["", "임계값 후보의 \"못 답할 질문\"은 정답이 빠진 질문과 정답 없는 질문을 합친 것이다. 표본이 작으니 후보일 뿐 확정값이 아니다."]
    return "\n".join(lines)


# --- 캐시 ------------------------------------------------------------------------


class JsonCache:
    """키 → JSON 값을 파일 하나에 저장하는 작은 캐시. 쓸 때마다 파일을 바로 갱신한다.

    왜 즉시 저장인가: 이 캐시가 아끼는 것은 무료 한도(임베딩·리랭크 호출)다. 마지막에 한꺼번에 저장하면 도중에 멈췄을 때
    이미 쓴 호출이 전부 사라진다. 임시 파일에 쓴 뒤 바꿔치기(os.replace)해서 쓰는 도중 멈춰도 파일이 반쯤 쓰이지 않는다.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data: dict[str, Any] = {}
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"캐시 파일이 깨졌습니다: {self._path} (지우고 다시 실행하세요. 이미 쓴 호출은 다시 쓰게 됩니다)") from exc
            if not isinstance(loaded, dict):
                raise ValueError(f"캐시 파일 모양이 틀립니다: {self._path}")
            self._data = loaded

    def get(self, key: str) -> Any | None:
        """키에 저장된 값을 돌려준다. 없으면 None."""
        return self._data.get(key)

    def set(self, key: str, value: Any) -> None:
        """값을 저장하고 파일에 바로 반영한다(임시 파일에 쓴 뒤 바꿔치기)."""
        self._data[key] = value
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(temp, "w", encoding="utf-8", newline="\n") as file:
            json.dump(self._data, file, ensure_ascii=False)
        os.replace(temp, self._path)


class CachedEmbedder:
    """질문 임베딩을 캐시하는 임베더(EmbedderPort). 같은 글은 다시 부르지 않는다.

    namespace: 모델·차원 같은 "벡터를 바꾸는 조건". 다르면 다른 칸을 쓴다(모델을 바꿨는데 옛 벡터를 쓰는 사고를 막는다).
    """

    def __init__(self, inner: EmbedderPort, cache: JsonCache, *, namespace: str) -> None:
        self._inner = inner
        self._cache = cache
        self._namespace = namespace
        self.live_calls = 0
        self.cache_hits = 0

    def embed_query(self, text: str) -> list[float]:
        """캐시에 있으면 그 벡터를, 없으면 진짜 임베더를 불러 얻은 벡터를 저장하고 돌려준다."""
        key = f"embed|{self._namespace}|{text}"
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        vector = self._inner.embed_query(text)
        self.live_calls += 1
        self._cache.set(key, vector)
        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """문서 임베딩은 이 측정에서 쓰지 않는다. 포트를 채우려고 진짜 임베더에 그대로 넘긴다."""
        return self._inner.embed_documents(texts)


class CachedRerankStage:
    """리랭크 결과를 캐시하고 진짜 호출 사이에 간격을 두는 RerankStage.

    캐시 열쇠: (질문, 후보 청크 ID 목록, top_n). 후보가 하나라도 다르면 다른 칸이다 — 깊이를 바꾸면 후보가 달라질 수 있어서다.
    리랭커를 실제로 쓴 결과(reason "ok")만 저장한다. 한도(cap)·오류(error)로 RRF 순서를 그대로 쓴 결과는 저장하지 않아서
    다시 실행하면 진짜 리랭크를 다시 시도한다.
    """

    def __init__(
        self,
        inner: RerankStage,
        cache: JsonCache,
        *,
        min_interval_s: float = COHERE_MIN_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last_live: float | None = None
        self.live_calls = 0
        self.cache_hits = 0

    @staticmethod
    def _key(query: str, hits: list[Hit], top_n: int) -> str:
        """(질문, 후보 청크 ID 목록, top_n)을 하나의 해시 열쇠로 만든다. \x00 구분자는 글자가 이어붙어 같아지는 일을 막는다."""
        raw = "\x00".join([query, str(top_n), *[h.chunk.chunk_id for h in hits]])
        return "rerank|" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def rerank(self, query: str, hits: list[Hit], top_n: int = TOP_N) -> RerankResult:
        """캐시에 있으면 그 결과를, 없으면 간격을 지켜 진짜 리랭크를 부른 뒤 성공한 결과만 저장한다."""
        key = self._key(query, hits, top_n)
        cached = self._cache.get(key)
        if cached is not None:
            by_id = {h.chunk.chunk_id: h for h in hits}
            rebuilt = [
                dataclasses.replace(by_id[chunk_id], rank=rank, score=score)
                for rank, (chunk_id, score) in enumerate(cached["items"], start=1)
            ]
            self.cache_hits += 1
            return RerankResult(rebuilt, True, "ok")

        if self._last_live is not None:
            wait = self._min_interval_s - (self._clock() - self._last_live)
            if wait > 0:
                self._sleep(wait)
        self._last_live = self._clock()
        result = self._inner.rerank(query, hits, top_n)
        self.live_calls += 1
        if result.used_reranker and result.reason == "ok":
            self._cache.set(key, {"reason": "ok", "items": [[h.chunk.chunk_id, h.score] for h in result.hits]})
        return result


# --- 명령줄 ----------------------------------------------------------------------


def _parse_depths(text: str) -> list[int]:
    """`--depths 100,150,200` 같은 쉼표 목록을 정수 목록으로 바꾼다. 틀리면 argparse가 사용법과 함께 알려 준다."""
    depths = [int(part) for part in text.split(",") if part.strip()]
    if not depths or any(d < 1 for d in depths):
        raise argparse.ArgumentTypeError("깊이는 1 이상의 정수를 쉼표로 이어서 적습니다(예: 100,150,200)")
    return depths


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    embedder: EmbedderPort | None = None,
    store_factory: Callable[[int], Any] | None = None,
    rerank_stage: RerankStage | None = None,
) -> int:
    """측정 본체. env·embedder·store_factory·rerank_stage는 테스트에서 가짜를 끼우려고 바꿀 수 있다(없으면 진짜를 만든다)."""
    parser = argparse.ArgumentParser(description="평가셋으로 검색 품질(Recall@k, MRR, 리랭크 폴백 비율)을 잰다")
    parser.add_argument("--evalset", default=str(ROOT / "eval" / "evalset.jsonl"))
    parser.add_argument("--k", type=int, default=20, help="Recall@k의 k이자 검색으로 가져올 개수")
    parser.add_argument("--field", choices=["both", *FIELDS], default="both", help="어느 문장으로 검색할지")
    parser.add_argument("--depths", type=_parse_depths, default=list(DEFAULT_DEPTHS), help="비교할 하위 쿼리 깊이(쉼표)")
    parser.add_argument("--rerank", action="store_true", help="리랭크 측정도 한다(Cohere 호출 사용)")
    parser.add_argument("--rerank-depth", type=int, help="리랭크를 잴 깊이(기본: 깊이 비교에서 가장 나은 값)")
    parser.add_argument("--rerank-interval", type=float, default=COHERE_MIN_INTERVAL_S, help="진짜 리랭크 호출 사이 최소 간격(초)")
    parser.add_argument("--cache-dir", default=str(ROOT / "data"), help="임베딩·리랭크 캐시와 평가용 DB를 둘 폴더")
    parser.add_argument("--out", help="요약과 기록을 JSON으로 저장할 경로(선택)")
    args = parser.parse_args(argv)
    env = _read_env() if env is None else env

    try:
        items = load_evalset(args.evalset)
    except (OSError, ValueError) as exc:
        print(f"오류: 평가셋을 읽을 수 없습니다: {exc}")
        return 1
    fields = list(FIELDS) if args.field == "both" else [args.field]
    cache_dir = Path(args.cache_dir)

    # 필요한 값이 없으면 한도를 쓰기 전에 알리고 멈춘다.
    missing = [
        name
        for name, needed in (
            ("LABOR_GEMINI_API_KEY", embedder is None),
            ("LABOR_OPENSEARCH_PASSWORD", store_factory is None),
            ("LABOR_COHERE_API_KEY", args.rerank and rerank_stage is None),
        )
        if needed and not env.get(name)
    ]
    if missing:
        print(f"오류: .env 또는 환경 변수에 {', '.join(missing)} 값이 없습니다.")
        return 1

    from app.config import Settings

    model = env.get("LABOR_EMBEDDING_MODEL", Settings.model_fields["embedding_model"].default)
    dim = int(env.get("LABOR_EMBEDDING_DIM", Settings.model_fields["embedding_dim"].default))
    if embedder is None:
        from app.adapters.gemini import GeminiEmbedder

        embedder = GeminiEmbedder(env["LABOR_GEMINI_API_KEY"], model=model, dim=dim)
    if store_factory is None:
        from app.adapters.opensearch_store import OpenSearchStore, make_client

        url = env.get("LABOR_OPENSEARCH_URL", Settings.model_fields["opensearch_url"].default)
        client = make_client(url, env.get("LABOR_OPENSEARCH_USER", Settings.model_fields["opensearch_user"].default),
                             env["LABOR_OPENSEARCH_PASSWORD"])
        index_name = env.get("LABOR_INDEX_NAME", Settings.model_fields["index_name"].default)
        store_factory = lambda depth: OpenSearchStore(client, index_name, depth=depth)  # noqa: E731
        print(f"검색 대상: {url} 의 '{index_name}' (임베딩 {model}, {dim}차원)")

    cached_embedder = CachedEmbedder(embedder, JsonCache(cache_dir / "embed_cache.json"), namespace=f"{model}|{dim}")
    gate_store = None
    if args.rerank:
        if rerank_stage is None:
            from app.adapters.cohere_rerank import CohereReranker
            from app.adapters.sqlite_store import AppStore
            from app.retrieval.rerank_gate import RerankGate

            # 평가가 쓰는 리랭크 월 카운터는 앱 DB가 아니라 이 PC의 평가용 DB에 따로 센다.
            gate_store = AppStore(str(cache_dir / "eval.db"))
            rerank_stage = RerankGate(CohereReranker(env["LABOR_COHERE_API_KEY"]), gate_store)
        cached_rerank = CachedRerankStage(rerank_stage, JsonCache(cache_dir / "rerank_cache.json"),
                                          min_interval_s=args.rerank_interval)

    def run(depth: int, run_items: Sequence[EvalItem], stage: RerankStage | None) -> list[Record]:
        store = store_factory(depth)

        def search(text: str) -> list[Hit]:
            return store.search(text, cached_embedder.embed_query(text), size=args.k)

        records: list[Record] = []
        for field in fields:
            records += evaluate_retrieval(run_items, field=field, search=search, rerank=stage, top_n=TOP_N)
        return records

    started = time.monotonic()
    try:
        with_gold = [i for i in items if i.gold]  # 깊이 비교는 정답 있는 질문만(정답 없는 질문은 Recall·MRR 대상이 아니다)
        by_depth: dict[int, list[Summary]] = {}
        all_records: dict[int, list[Record]] = {}
        for depth in args.depths:
            records = run(depth, with_gold, None)
            all_records[depth] = records
            by_depth[depth] = [summarize(records, field=f, k=args.k, top_n=TOP_N) for f in fields]

        rerank_summaries = None
        rerank_depth = None
        if args.rerank:
            rerank_depth = args.rerank_depth or best_depth(by_depth)
            rerank_records = run(rerank_depth, items, cached_rerank)  # 정답 없는 질문도 포함: 점수 분포의 "못 답할 질문" 쪽 재료
            rerank_summaries = [summarize(rerank_records, field=f, k=args.k, top_n=TOP_N) for f in fields]
    except QuotaExceeded as exc:
        wait = f" 서버가 알려 준 대기 시간 약 {exc.retry_after_s:.0f}초." if getattr(exc, "retry_after_s", None) else ""
        print(f"무료 한도 초과로 멈췄습니다.{wait} 지금까지의 호출은 캐시에 저장됐으니 시간을 두고 다시 실행하면 이어서 합니다.")
        return 2

    print(format_report(by_depth, rerank_summaries=rerank_summaries, k=args.k, top_n=TOP_N))
    print()
    print(f"가장 나은 깊이(리랭크 없이 비교): {best_depth(by_depth)}" + (f" / 리랭크를 잰 깊이: {rerank_depth}" if rerank_depth else ""))
    print(f"임베딩 호출 {cached_embedder.live_calls}회(캐시 {cached_embedder.cache_hits}회)"
          + (f" · 리랭크 호출 {cached_rerank.live_calls}회(캐시 {cached_rerank.cache_hits}회)" if args.rerank else "")
          + f" · 소요 {time.monotonic() - started:.1f}초")
    if gate_store is not None:
        from datetime import date

        print(f"이 PC의 평가용 DB가 센 이번 달 리랭크 호출 수: {gate_store.get_rerank_calls(date.today().strftime('%Y-%m'))}"
              " (실제 Cohere 월 한도는 서버 앱 호출과 합산되니 서버 카운터와는 별개임에 주의)")

    if args.out:
        payload = {
            "k": args.k,
            "depths": {str(d): [dataclasses.asdict(s) for s in by_depth[d]] for d in by_depth},
            "rerank_depth": rerank_depth,
            "rerank": [dataclasses.asdict(s) for s in rerank_summaries] if rerank_summaries else None,
        }
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"요약을 {args.out} 에 저장했습니다.")
    return 0


def _read_env() -> dict[str, str]:
    """`.env` + 환경 변수를 읽는다(다른 스크립트와 같은 방법). 가져오기를 함수 안에 둔 이유: 시험에서 env를 직접 넘기면 이 파일을 건드리지 않는다."""
    from scripts.envfile import read_env

    return read_env()


if __name__ == "__main__":
    sys.exit(main())
