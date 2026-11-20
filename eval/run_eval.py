"""전체 평가 러너: 평가셋의 질문을 서비스(그래프)에 끝까지 돌려 답변 단계 지표를 낸다 (Task 17).

Task 9의 `eval/run_retrieval.py`는 검색기만 따로 쟀다(질문을 그대로 검색에 쓴다). 이 러너는 서버와 **똑같은 그래프**
(질문 분석 → 검색 → 리랭크 → 충분성 → 생성 → 코드 검사 → LLM 검증 → 통과/재시도/거절)를 질문마다 돌리고,
그 결과(답변했는가, 어떤 조문을 인용했는가, LLM을 몇 번 불렀는가, 얼마나 걸렸는가)를 지표로 만든다.

사용법(프로젝트 루트에서, 서버의 OpenSearch는 SSH 터널로 연다 — deploy/opensearch-notes.md 6절):
    python -m eval.run_eval --limit 15 --model gemini-3.5-flash-lite --out data/results_flash-lite.json   # 모델 비교용 표본
    python -m eval.run_eval --out data/results.json --report docs/results.md                              # 전체 평가 + 리포트
    python -m eval.run_eval --out data/results.json --resume                                             # 한도로 멈춘 뒤 이어서
    python -m eval.run_eval --out data/results.json --resume --export-validator data/validator_sheet.csv # 검증기 라벨 시트 만들기
    python -m eval.run_eval --out data/results.json --resume --validator-sheet data/validator_sheet.csv --report docs/results.md

무엇을 어떻게 재나
- 기본으로 `casual_question`(일상어 질문)을 쓴다. 실제 사용에 더 가까운 문장이라서다(조문 어휘형은 점수가 부풀려진다).
- 같은 임베딩·리랭크 캐시(data/)를 쓴다. 다만 서비스는 LLM이 질문을 다시 쓴 검색어로 검색하므로, LLM 출력이 같지 않으면
  캐시에 안 걸리고 진짜 호출이 나간다. 진짜 Cohere 호출 사이에는 분당 10회 한도 때문에 간격(기본 6.5초)을 둔다.
- **지연 시간은 그 간격 대기를 뺀 값이다.** 안 빼면 평가 때문에 일부러 쉰 시간이 p95에 섞여 실제 서비스보다 훨씬 느려 보인다.
- Langfuse로는 보내지 않는다(월 50,000 units를 평가가 쓰지 않게).

결과 파일(--out)에는 질문별 결과와 설정(모델, 문장 종류)이 들어 있다. 이미 있으면 덮어쓰지 않고 멈춘다 — 모델이나 프롬프트를
바꿨는데 옛 결과가 섞이는 사고를 막기 위해서다. 한도로 멈춘 평가를 이어서 하려면 `--resume`(같은 모델·문장 종류만 허용).

종료 코드: 0 성공, 1 설정·입력 문제, 2 무료 한도 초과 또는 외부 서비스 오류로 중단(결과는 저장됨, --resume으로 이어서 한다).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

# `python -m eval.run_eval`은 프로젝트 루트가 이미 경로에 있다. 직접 실행(`python eval/run_eval.py`)도 되게 넣어 둔다.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.domain.errors import ExternalServiceError, QuotaExceeded  # noqa: E402
from app.domain.models import GoldKey  # noqa: E402
from app.graph.runner import Final, run_chat  # noqa: E402
from eval.evalset import EvalItem, article_sort_key, load_evalset  # noqa: E402
from eval.labels import export_validator_sheet, predicted_flags, read_validator_sheet  # noqa: E402
from eval.metrics import AnswerSentence, ItemResult, compute_metrics, validator_accuracy  # noqa: E402
from eval.report import render_report  # noqa: E402
from eval.run_retrieval import (  # noqa: E402
    COHERE_MIN_INTERVAL_S,
    FIELD_NAMES,
    FIELDS,
    CachedEmbedder,
    CachedRerankStage,
    JsonCache,
)

DEFAULT_FIELD = "casual_question"

# 평가가 따로 쓰지 않는 웹 서비스 전용 필수 설정(로그인 해시, 세션 비밀, Langfuse 키, 법제처 API 값)에 넣는 자리표시 값.
# Settings가 이 항목들을 필수로 요구하지만 평가 러너는 쓰지 않는다(비밀값이 아니다).
UNUSED = "unused-in-eval"


# --- 질문 고르기 -----------------------------------------------------------------


def sample_items(items: Sequence[EvalItem], n: int) -> list[EvalItem]:
    """평가셋에서 n건을 **고르게** 뽑는다(모델 비교에서 같은 표본을 쓰려고 항상 같은 결과를 준다).

    평가셋은 라벨 순서(답할 수 있는 질문 → 사건 판단 → 근거 없음 → 범위 밖)로 정렬되어 있어서, 앞에서 n건만 자르면 거절
    지표를 못 재는 표본이 된다. 처음부터 끝까지 같은 간격으로 뽑아 모든 라벨이 들어가게 한다. n이 전체 이상이면 전부다.
    """
    if n < 1:
        raise ValueError("n은 1 이상이어야 합니다")
    if n >= len(items):
        return list(items)
    if n == 1:
        return [items[0]]
    step = (len(items) - 1) / (n - 1)
    # int(x + 0.5)는 "반올림(0.5는 올림)"이다. 간격이 1 이상이라 서로 다른 칸이 뽑힌다.
    return [items[int(i * step + 0.5)] for i in range(n)]


# --- 질문 하나 돌리기 ------------------------------------------------------------


def source_key(source: dict) -> GoldKey | None:
    """화면용 출처 한 건(`answer_payload`의 sources 원소)을 정답과 비교할 (법령명, 조문번호)로 바꾼다.

    출처는 조문번호와 가지번호를 나눠서 갖고 있지만(`43`, `2`) 정답과 색인은 "43의2" 한 문자열을 쓴다(Task 4). 그래서 다시 합친다.
    법령명이나 조문번호가 없으면 키를 만들 수 없어 None이다.
    """
    law, article, branch = source.get("law_name"), source.get("article_no"), source.get("branch_no")
    if not law or not article:
        return None
    return (law, f"{article}의{branch}" if branch else article)


def _verifier_flags(values: Mapping[str, Any], count: int) -> list[bool | None]:
    """나간 문장 `count`개 각각에 대한 검증기 판정(True=일치)을 그래프의 마지막 상태에서 꺼낸다. 알 수 없으면 None.

    나간 문장이 생성된 문장보다 1개 적은 경우가 있다: 사건 판단 요청의 서두 문장을 모델이 첫 문장에 똑같이 썼으면
    화면용 값(answer_payload)이 그 문장을 빼기 때문이다. 그때는 앞의 판정 하나를 버려 위치를 맞춘다.
    """
    answer, verification = values.get("answer"), values.get("verification")
    if answer is None or verification is None:
        return [None] * count
    flags = list(verification.sentences_ok)
    offset = len(answer.sentences) - count
    if offset not in (0, 1) or len(flags) != len(answer.sentences):
        return [None] * count
    return flags[offset:]


def run_item(
    graph: Any,
    item: EvalItem,
    text: str,
    *,
    clock: Callable[[], float] = time.monotonic,
    slept: Callable[[], float] = lambda: 0.0,
) -> ItemResult:
    """질문 하나(`text`)를 새 대화로 그래프에 끝까지 돌리고 결과를 모은다.

    graph: 체크포인터가 달린 컴파일된 그래프(서버와 같은 `build_graph`의 결과). 항목마다 새 thread_id를 써서 앞 질문의
    대화 이력이나 호출 수가 이어지지 않는다. 끝난 뒤의 마지막 상태에서 LLM 호출 수·리랭커 사용·검증 결과를 읽는다.
    clock·slept: 걸린 시간을 재는 시계와 "그동안 일부러 쉰 누적 시간"(둘 다 시험에서 바꿔 끼운다). 지연 시간 = 걸린 시간 − 쉰 시간.
    한도 초과·외부 서비스 오류는 잡지 않고 올린다(러너 main이 멈춤 처리를 한다).
    """
    thread_id = str(uuid.uuid4())
    started, slept_before = clock(), slept()
    payload: dict | None = None
    for event in run_chat(graph, thread_id, text):
        if isinstance(event, Final):
            payload = event.payload
    latency = (clock() - started) - (slept() - slept_before)
    assert payload is not None  # run_chat이 Final 없이 끝나면 RuntimeError를 이미 냈다

    values = graph.get_state({"configurable": {"thread_id": thread_id}}).values
    verification = values.get("verification")
    refused = bool(payload["refused"])
    # 거절 payload의 sentences는 항상 비어 있어서(refusal_payload) 거절한 질문에는 문장이 남지 않는다.
    flags = _verifier_flags(values, len(payload["sentences"]))
    sentences = tuple(
        AnswerSentence(s["text"], tuple(s["citation_ids"]), flag) for s, flag in zip(payload["sentences"], flags)
    )
    return ItemResult(
        item_id=item.id,
        label=item.label,
        gold=item.gold,
        refused=refused,
        cited=frozenset(key for key in map(source_key, payload["sources"]) if key is not None),
        llm_calls=int(values.get("llm_calls", 0)),
        latency_s=latency,
        used_reranker=bool(values.get("used_reranker")),
        verdict_found=bool(verification.verdict_found) if verification is not None else False,
        regen_count=int(values.get("regen_count", 0)),
        research_count=int(values.get("research_count", 0)),
        reranked_count=len(values.get("reranked", [])),
        question=text,
        sentences=sentences,
        sources={s["id"]: f"{s['label']}: {s['text']}" for s in payload["sources"]},
    )


# --- 결과 파일 -------------------------------------------------------------------


def _result_to_json(result: ItemResult) -> dict:
    """결과 하나를 JSON으로 쓸 수 있는 dict로 바꾼다. 집합은 정렬한 목록으로(같은 결과는 같은 글이 되도록)."""
    return {
        "item_id": result.item_id,
        "label": result.label,
        "gold": [list(k) for k in sorted(result.gold, key=article_sort_key)],
        "refused": result.refused,
        "cited": [list(k) for k in sorted(result.cited, key=article_sort_key)],
        "llm_calls": result.llm_calls,
        "latency_s": result.latency_s,
        "used_reranker": result.used_reranker,
        "verdict_found": result.verdict_found,
        "regen_count": result.regen_count,
        "research_count": result.research_count,
        "reranked_count": result.reranked_count,
        "question": result.question,
        "sentences": [
            {"text": s.text, "citation_ids": list(s.citation_ids), "verifier_ok": s.verifier_ok} for s in result.sentences
        ],
        "sources": result.sources,
    }


def _result_from_json(raw: dict) -> ItemResult:
    """`_result_to_json`의 반대. 모양이 틀리면 KeyError/TypeError가 나므로 `load_results`가 ValueError로 바꿔 알린다."""
    return ItemResult(
        item_id=raw["item_id"],
        label=raw["label"],
        gold=frozenset((law, article) for law, article in raw["gold"]),
        refused=raw["refused"],
        cited=frozenset((law, article) for law, article in raw["cited"]),
        llm_calls=raw["llm_calls"],
        latency_s=raw["latency_s"],
        used_reranker=raw["used_reranker"],
        verdict_found=raw["verdict_found"],
        regen_count=raw["regen_count"],
        research_count=raw["research_count"],
        reranked_count=raw["reranked_count"],
        question=raw["question"],
        sentences=tuple(AnswerSentence(s["text"], tuple(s["citation_ids"]), s["verifier_ok"]) for s in raw["sentences"]),
        sources=dict(raw["sources"]),
    )


def save_results(path: str | Path, results: Sequence[ItemResult], meta: Mapping[str, Any]) -> None:
    """결과와 설정(meta)을 JSON 파일 하나로 저장한다. 임시 파일에 쓴 뒤 바꿔치기해서, 쓰는 도중 멈춰도 파일이 반쯤 쓰이지 않는다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    body = {"meta": dict(meta), "results": [_result_to_json(r) for r in results]}
    with open(temp, "w", encoding="utf-8", newline="\n") as file:
        json.dump(body, file, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def load_results(path: str | Path) -> tuple[dict, list[ItemResult]]:
    """결과 파일을 (설정, 결과 목록)으로 읽는다. 파일이 깨졌거나 모양이 틀리면 무엇이 문제인지 알려 주며 ValueError."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return dict(raw["meta"]), [_result_from_json(r) for r in raw["results"]]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"결과 파일을 읽을 수 없습니다: {path} ({type(exc).__name__}: {exc})") from exc


# --- 실행 환경 -------------------------------------------------------------------


class SleepMeter:
    """`time.sleep`을 감싸서 쉰 시간을 누적하는 도구. 리랭크 호출 사이의 간격 대기를 지연 시간에서 빼려고 쓴다."""

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep
        self.total = 0.0

    def sleep(self, seconds: float) -> None:
        """실제로 쉬고, 쉰 시간을 더한다."""
        self._sleep(seconds)
        self.total += seconds

    def slept(self) -> float:
        """지금까지 쉰 시간의 합(초)."""
        return self.total


def _read_env() -> dict[str, str]:
    """`.env` + 환경 변수를 읽는다(다른 스크립트와 같은 방법). 가져오기를 함수 안에 둔 이유: 시험에서 env를 직접 넘기면 건드리지 않는다."""
    from scripts.envfile import read_env

    return read_env()


def _eval_settings(env: Mapping[str, str], model: str | None):
    """진짜 그래프를 만들 Settings를 환경 변수에서 만든다. 평가가 쓰지 않는 필수 항목에는 자리표시 값(UNUSED)을 넣는다."""
    from app.config import Settings

    values: dict[str, Any] = {
        "gemini_api_key": env["LABOR_GEMINI_API_KEY"],
        "cohere_api_key": env["LABOR_COHERE_API_KEY"],
        "opensearch_password": env["LABOR_OPENSEARCH_PASSWORD"],
        **{name: UNUSED for name in (
            "law_api_oc", "admin_password_hash", "test_password_hash", "session_secret",
            "langfuse_public_key", "langfuse_secret_key", "langfuse_host",
        )},
    }
    # 서비스와 같은 값으로 돌려야 하므로, 환경 변수에 있는 조정값은 그대로 따른다(없으면 Settings 기본값).
    for name in ("embedding_model", "embedding_dim", "llm_model", "max_llm_calls", "max_regen", "max_research",
                 "search_depth", "rerank_min_score", "rerank_monthly_cap", "opensearch_url", "opensearch_user", "index_name"):
        if env.get(f"LABOR_{name.upper()}"):
            values[name] = env[f"LABOR_{name.upper()}"]
    if model:
        values["llm_model"] = model
    return Settings(_env_file=None, **values)


def _build_real_graph(env: Mapping[str, str], args: argparse.Namespace, meter: SleepMeter):
    """진짜 부품(Gemini·OpenSearch·Cohere)으로 서버와 같은 그래프를 만든다. (그래프, 호출 통계를 돌려주는 함수)를 돌려준다.

    임베딩·리랭크는 캐시를 한 겹 씌운다(무료 한도를 아끼고, 도중에 멈춰도 이미 쓴 호출을 잃지 않는다). 대화 상태 저장소는
    메모리다(평가 질문은 서로 이어지지 않는다). Langfuse는 연결하지 않는다.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    from app.adapters.sqlite_store import AppStore
    from app.container import build_deps
    from app.graph.build import build_graph
    from app.graph.state import checkpoint_serde

    settings = _eval_settings(env, args.model)
    cache_dir = Path(args.cache_dir)
    gate_store = AppStore(str(cache_dir / "eval.db"))  # 리랭크 월 카운터는 앱 DB가 아니라 평가용 DB에 따로 센다
    deps = build_deps(settings, gate_store, tracing=None)
    embedder = CachedEmbedder(
        deps.embedder, JsonCache(cache_dir / "embed_cache.json"), namespace=f"{settings.embedding_model}|{settings.embedding_dim}"
    )
    rerank = CachedRerankStage(
        deps.rerank, JsonCache(cache_dir / "rerank_cache.json"), min_interval_s=args.rerank_interval, sleep=meter.sleep
    )
    graph = build_graph(dataclasses.replace(deps, embedder=embedder, rerank=rerank), InMemorySaver(serde=checkpoint_serde()))

    def stats() -> str:
        """실행 뒤에 보여 줄 호출 통계 한 줄: 임베딩·리랭크의 진짜 호출 수와 캐시 적중 수, 이번 달 리랭크 호출 수."""
        from datetime import date

        month_calls = gate_store.get_rerank_calls(date.today().strftime("%Y-%m"))
        return (
            f"임베딩 호출 {embedder.live_calls}회(캐시 {embedder.cache_hits}회) · 리랭크 호출 {rerank.live_calls}회(캐시 {rerank.cache_hits}회)"
            f" · 이 PC 평가용 DB가 센 이번 달 리랭크 호출 수 {month_calls}"
            f"(실제 Cohere 월 한도 {settings.rerank_monthly_cap}회는 서버 앱 호출과 합산되니 서버 카운터와는 별개)"
        )

    return graph, stats


def _notes(field: str, model: str, run_count: int, evalset_count: int, reviewed: int) -> list[str]:
    """리포트에 붙이는 한계·조건. 숫자만 보고 잘못 읽지 않게, 이 평가가 무엇을 재고 무엇을 못 재는지 적는다."""
    return [
        f"질문 문장: {field}({FIELD_NAMES[field]}), LLM 모델: {model}, 평가셋 {evalset_count}건 중 {run_count}건을 실행했다.",
        f"평가셋은 LLM이 법령 조문을 읽고 쓴 초안이다. 사람이 확인한 일상어 질문은 {reviewed}건이다(사용자가 검수를 생략하기로 했다).",
        "정답 조문은 질문을 만들 때 쓴 조문이라, 조문 어휘와 겹치는 질문일수록 점수가 실제보다 높게 나올 수 있다.",
        "결론 문장이 없는 비율은 구조상 높게 나온다: 서비스는 검증기가 결론 문장을 찾은 답변을 내보내지 않고 다시 쓰거나 거절한다. "
        "검증기가 실제로 걸러낸 정도는 '답변을 다시 쓴 질문의 비율'이 보여 준다.",
        "지연 시간은 Cohere 분당 한도 때문에 평가가 일부러 쉰 시간을 뺀 값이다.",
    ]


# --- 명령줄 ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None, graph: Any = None) -> int:
    """평가 본체. env·graph는 시험에서 가짜를 끼우려고 바꿀 수 있다(graph를 주면 진짜 부품을 만들지 않는다)."""
    parser = argparse.ArgumentParser(description="평가셋의 질문을 서비스(그래프)에 끝까지 돌려 답변 단계 지표를 낸다")
    parser.add_argument("--evalset", default=str(ROOT / "eval" / "evalset.jsonl"))
    parser.add_argument("--field", choices=FIELDS, default=DEFAULT_FIELD, help="어느 문장으로 질문할지(기본: 일상어형)")
    parser.add_argument("--model", help="LLM 모델 이름(기본: 설정의 llm_model)")
    parser.add_argument("--limit", type=int, help="평가셋에서 고르게 N건만 돌린다(모델 비교용)")
    parser.add_argument("--out", default=str(ROOT / "data" / "results.json"), help="결과를 저장할 JSON 경로")
    parser.add_argument("--resume", action="store_true", help="--out 파일의 끝난 항목은 건너뛰고 이어서 한다")
    parser.add_argument("--report", help="리포트(마크다운)를 저장할 경로(예: docs/results.md)")
    parser.add_argument("--export-validator", metavar="CSV", help="검증기 라벨 시트를 만든다(사람이 문장마다 O/X를 표시)")
    parser.add_argument("--validator-n", type=int, default=50, help="검증기 라벨 시트에 올릴 문장 수")
    parser.add_argument("--validator-sheet", metavar="CSV", help="사람이 채운 검증기 라벨 시트를 읽어 검증기 정확도를 낸다")
    parser.add_argument("--cache-dir", default=str(ROOT / "data"), help="임베딩·리랭크 캐시와 평가용 DB를 둘 폴더")
    parser.add_argument("--rerank-interval", type=float, default=COHERE_MIN_INTERVAL_S, help="진짜 리랭크 호출 사이 최소 간격(초)")
    args = parser.parse_args(argv)
    env = _read_env() if env is None else env

    try:
        items = load_evalset(args.evalset)
    except (OSError, ValueError) as exc:
        print(f"오류: 평가셋을 읽을 수 없습니다: {exc}")
        return 1
    if args.limit is not None and args.limit < 1:
        print("오류: --limit은 1 이상이어야 합니다.")
        return 1

    if graph is None:
        # 필요한 값이 없으면 무료 한도를 쓰기 전에 알리고 멈춘다.
        missing = [n for n in ("LABOR_GEMINI_API_KEY", "LABOR_COHERE_API_KEY", "LABOR_OPENSEARCH_PASSWORD") if not env.get(n)]
        if missing:
            print(f"오류: .env 또는 환경 변수에 {', '.join(missing)} 값이 없습니다.")
            return 1

    from app.config import Settings

    model = args.model or env.get("LABOR_LLM_MODEL") or Settings.model_fields["llm_model"].default
    meta = {"model": model, "field": args.field}

    # 이어서 하기 / 덮어쓰기 방지
    out = Path(args.out)
    results: list[ItemResult] = []
    if out.exists():
        if not args.resume:
            print(f"오류: {out} 이(가) 이미 있습니다. 이어서 하려면 --resume, 새로 하려면 다른 --out 이름을 쓰거나 파일을 옮겨 주세요.")
            return 1
        try:
            old_meta, results = load_results(out)
        except ValueError as exc:
            print(f"오류: {exc}")
            return 1
        if old_meta != meta:
            print(f"오류: {out} 은(는) 다른 설정으로 만든 결과입니다(파일: {old_meta}, 지금: {meta}). 지표가 섞이지 않게 멈춥니다.")
            return 1
    elif args.resume:
        print(f"안내: {out} 이(가) 없어서 처음부터 시작합니다.")

    runnable = [i for i in items if getattr(i, args.field)]  # 그 문장이 없는 항목(검수 반려)은 건너뛴다 — 다른 문장으로 대신하면 섞인다
    if args.limit is not None:
        runnable = sample_items(runnable, args.limit)
    done_ids = {r.item_id for r in results}
    todo = [i for i in runnable if i.id not in done_ids]

    meter = SleepMeter()
    stats: Callable[[], str] | None = None
    if todo:
        if graph is None:
            graph, stats = _build_real_graph(env, args, meter)
        print(f"LLM {model} · 질문 {len(runnable)}건 중 {len(todo)}건 실행(문장: {args.field})")
    started = time.monotonic()
    try:
        for item in todo:
            result = run_item(graph, item, getattr(item, args.field), slept=meter.slept)
            results.append(result)
            save_results(out, results, meta)  # 한 건 끝날 때마다 저장: 도중에 멈춰도 이미 쓴 호출을 잃지 않는다
            print(f"[{len(results)}/{len(runnable)}] {item.id} {'거절' if result.refused else '답변'} · LLM {result.llm_calls}회 · {result.latency_s:.1f}초")
    except (QuotaExceeded, ExternalServiceError) as exc:
        reason = "무료 한도 초과" if isinstance(exc, QuotaExceeded) else f"외부 서비스 오류({type(exc).__name__})"
        wait = f" 서버가 알려 준 대기 시간 약 {exc.retry_after_s:.0f}초." if getattr(exc, "retry_after_s", None) else ""
        print(f"{reason}로 멈췄습니다.{wait} {len(results)}건은 {out} 에 저장됐으니 시간을 두고 --resume으로 이어서 하세요.")
        return 2

    if args.export_validator:
        try:
            export_validator_sheet(results, args.export_validator, n=args.validator_n)
        except FileExistsError as exc:
            print(f"오류: {exc}")
            return 1
        print(f"검증기 라벨 시트를 {args.export_validator} 에 만들었습니다. 문장마다 일치(O/X)와 검수자(본인 이름)를 채워 저장하세요.")

    metrics = compute_metrics(results)
    human_checked = sum(1 for i in runnable if i.reviewer)
    if args.validator_sheet:
        try:
            human = read_validator_sheet(args.validator_sheet)
            predicted = predicted_flags(results, list(human))
        except (OSError, ValueError, KeyError) as exc:
            print(f"오류: 검증기 라벨 시트를 쓸 수 없습니다: {exc}")
            return 1
        if human:
            metrics["validator_accuracy"] = validator_accuracy(predicted, list(human.values()))
            metrics["validator_labels"] = len(human)
            human_checked += len(human)

    mean_calls, max_calls = metrics["llm_calls_mean"], metrics["llm_calls_max"]
    if mean_calls is not None:
        print(f"질문당 LLM 호출 평균 {mean_calls:.1f}회(최대 {max_calls}회) · 소요 {time.monotonic() - started:.1f}초")
    if stats is not None:
        print(stats())

    if args.report:
        report = render_report(
            metrics, human_checked,
            title="평가 리포트 (Task 17)",
            notes=_notes(args.field, model, len(runnable), len(items), sum(1 for i in runnable if i.reviewer)),
        )
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report, encoding="utf-8", newline="\n")
        print(f"리포트를 {args.report} 에 저장했습니다.")
    else:
        print(render_report(metrics, human_checked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
