"""검색 측정 러너(eval/run_retrieval.py) 시험 (Task 9).

진짜 OpenSearch·Gemini·Cohere는 쓰지 않는다. 측정 루프·요약·캐시가 올바른지를 가짜 부품으로 확인한다.
(진짜 서비스로 돌린 결과는 docs/step0-report.md에 따로 기록한다.)
"""

import dataclasses
import json

import pytest

from app.domain.errors import QuotaExceeded
from app.domain.models import Chunk, Hit, RerankResult
from eval.evalset import EvalItem
from eval.run_retrieval import (
    CachedEmbedder,
    CachedRerankStage,
    JsonCache,
    Record,
    Summary,
    best_depth,
    evaluate_retrieval,
    format_report,
    suggest_threshold,
    summarize,
)
from tests.conftest import FakeEmbedder, FakeRerankStage, sample_hits

LAW = "근로기준법"


def item(item_id, gold, *, label="answerable", question="질문", casual="일상어 질문") -> EvalItem:
    return EvalItem(item_id, question, casual, frozenset(gold), "요약", label)


def record(item_id, gold, ranked, *, reranked=None, score=None, reason=None, field="casual_question", label="answerable"):
    return Record(item_id, label, field, frozenset(gold), tuple(ranked), None if reranked is None else tuple(reranked),
                  score, reason)


class ListSearch:
    """질문과 상관없이 정해 둔 후보를 돌려주고, 받은 질문을 기록한다."""

    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def __call__(self, text):
        self.queries.append(text)
        return self.hits


# --- 평가 루프 ---------------------------------------------------------------


# 한 항목을 RRF 순서와 리랭크 뒤 순서로 각각 기록한다: 정답이 RRF 3등이면 리랭크 상위 2개(top_n=2)에는 안 들어간다.
def test_evaluate_records_rrf_order_and_reranked_order():
    search = ListSearch(sample_hits(5))  # 근로기준법 제51~55조
    records = evaluate_retrieval(
        [item("a01", {(LAW, "53")})], field="casual_question", search=search, rerank=FakeRerankStage(), top_n=2
    )
    r = records[0]
    assert r.ranked == ((LAW, "51"), (LAW, "52"), (LAW, "53"), (LAW, "54"), (LAW, "55"))
    assert r.reranked == ((LAW, "51"), (LAW, "52")) and r.rerank_reason == "ok"
    assert search.queries == ["일상어 질문"]  # 선택한 필드의 문장으로 검색했다


# 해당 필드의 문장이 없는 항목(검수에서 반려된 일상어 질문)은 건너뛴다. 조문 어휘형으로 대신 검색하면 결과가 섞인다.
def test_item_without_the_selected_phrasing_is_skipped():
    items = [dataclasses.replace(item("a01", {(LAW, "51")}), casual_question=None), item("a02", {(LAW, "51")})]
    records = evaluate_retrieval(items, field="casual_question", search=ListSearch(sample_hits(2)), rerank=None)
    assert [r.item_id for r in records] == ["a02"] and records[0].reranked is None


# 리랭크 점수는 리랭커를 실제로 썼을 때만 기록한다(한도·오류로 RRF 순서를 그대로 쓴 경우는 점수가 의미 없다).
def test_top_score_is_recorded_only_when_the_reranker_was_really_used():
    used = evaluate_retrieval([item("a01", {(LAW, "51")})], field="question", search=ListSearch(sample_hits(3)),
                              rerank=FakeRerankStage(), top_n=2)
    fallback = evaluate_retrieval([item("a01", {(LAW, "51")})], field="question", search=ListSearch(sample_hits(3)),
                                  rerank=FakeRerankStage(used_reranker=False, reason="cap"), top_n=2)
    assert used[0].top_score == 0.9
    assert fallback[0].top_score is None and fallback[0].rerank_reason == "cap"


# --- 요약 --------------------------------------------------------------------


# 정답 있는 항목만 Recall·MRR 평균에 들어간다: 정답 없는 거절 항목이 평균을 깎으면 안 된다.
def test_summarize_averages_only_items_with_gold():
    records = [
        record("a01", {(LAW, "55")}, [(LAW, "55")]),                                  # recall 1, mrr 1
        record("a02", {(LAW, "56"), (LAW, "57")}, [("민법", "1"), (LAW, "56")]),      # recall 0.5, mrr 0.5
        record("o01", set(), [(LAW, "1")], label="out_of_scope"),                     # 정답 없음: 제외
    ]
    s = summarize(records, field="casual_question", k=20, top_n=5)
    assert (s.n, s.recall, s.mrr) == (2, 0.75, 0.75)


# 다른 필드의 기록은 섞이지 않는다.
def test_summarize_filters_by_field():
    records = [record("a01", {(LAW, "55")}, [(LAW, "55")], field="question"),
               record("a01", {(LAW, "55")}, [("민법", "1")], field="casual_question")]
    assert summarize(records, field="question", k=20, top_n=5).recall == 1.0
    assert summarize(records, field="casual_question", k=20, top_n=5).recall == 0.0


# 리랭크 뒤 지표(top_n 안의 재현율·MRR)와 폴백 비율. 폴백 비율의 분모는 리랭크를 시도한 항목 수다.
def test_summarize_rerank_stats_and_fallback_rate():
    records = [
        record("a01", {(LAW, "55")}, [(LAW, "51"), (LAW, "55")], reranked=[(LAW, "55")], score=0.9, reason="ok"),
        record("a02", {(LAW, "56")}, [(LAW, "56")], reranked=[(LAW, "56")], reason="error"),
        record("a03", {(LAW, "57")}, [(LAW, "57")], reranked=[(LAW, "57")], reason="cap"),
        record("a04", {(LAW, "58")}, [(LAW, "58")]),  # 리랭크를 시도하지 않은 기록
    ]
    s = summarize(records, field="casual_question", k=20, top_n=5)
    assert s.rerank is not None
    assert s.rerank.tried == 3 and s.rerank.fallback_rate == pytest.approx(2 / 3)
    assert s.rerank.recall == 1.0 and s.rerank.mrr == 1.0


# 리랭크 점수를 세 무리로 나눈다: 정답이 상위 N에 든 질문(hit), 정답이 있는데 빠진 질문(miss), 정답 없는 거절 질문(neg).
def test_summarize_groups_rerank_scores():
    records = [
        record("a01", {(LAW, "55")}, [(LAW, "55")], reranked=[(LAW, "55")], score=0.9, reason="ok"),
        record("a02", {(LAW, "56")}, [(LAW, "1")], reranked=[(LAW, "1")], score=0.4, reason="ok"),
        record("o01", set(), [(LAW, "1")], reranked=[(LAW, "1")], score=0.1, reason="ok", label="out_of_scope"),
    ]
    rerank = summarize(records, field="casual_question", k=20, top_n=5).rerank
    assert (rerank.scores_hit, rerank.scores_miss, rerank.scores_neg) == ((0.9,), (0.4,), (0.1,))


# --- 임계값 후보 ---------------------------------------------------------------


# 정답이 상위에 든 질문의 점수(높아야 함)와 정답 없는 질문의 점수(낮아야 함)를 가장 잘 가르는 값을 고른다.
def test_suggest_threshold_separates_the_two_groups():
    t, tpr, tnr = suggest_threshold([0.9, 0.8], [0.2, 0.1])
    assert 0.2 < t < 0.8 and (tpr, tnr) == (1.0, 1.0)


# 똑같이 잘 가르는 후보가 둘이면, 답할 수 있는 질문을 잘못 거절하지 않는 쪽(낮은 임계값)을 고른다.
def test_suggest_threshold_prefers_not_refusing_answerable_questions_on_ties():
    t, tpr, tnr = suggest_threshold([0.9, 0.8, 0.7], [0.2, 0.1, 0.75])
    assert t < 0.7 and tpr == 1.0


# 어느 한쪽 점수가 없으면 후보를 만들 수 없다.
def test_suggest_threshold_needs_both_groups():
    assert suggest_threshold([], [0.1]) is None and suggest_threshold([0.9], []) is None


# --- 깊이 선택 -----------------------------------------------------------------


def summary(field, recall, mrr):
    return Summary(field=field, n=10, recall=recall, mrr=mrr, rerank=None)


# 필드별 Recall의 평균이 가장 높은 깊이를 고른다. 같으면 MRR이 높은 쪽, 그래도 같으면 작은 깊이(메모리·속도가 유리).
def test_best_depth_rule():
    by_depth = {
        100: [summary("question", 0.80, 0.5), summary("casual_question", 0.60, 0.4)],
        150: [summary("question", 0.90, 0.5), summary("casual_question", 0.70, 0.4)],
        200: [summary("question", 0.90, 0.5), summary("casual_question", 0.70, 0.4)],
    }
    assert best_depth(by_depth) == 150  # 150과 200이 같으면 작은 쪽
    by_depth[200] = [summary("question", 0.90, 0.6), summary("casual_question", 0.70, 0.5)]
    assert best_depth(by_depth) == 200  # MRR이 더 높으면 큰 쪽이 이긴다


# --- 보고서 --------------------------------------------------------------------


# 보고서 표에 깊이별 Recall·MRR과 리랭크 폴백 비율이 사람이 읽는 형태로 들어간다.
def test_format_report_shows_depth_rows_and_fallback_rate():
    records = [record("a01", {(LAW, "55")}, [(LAW, "55")], reranked=[(LAW, "55")], score=0.9, reason="error")]
    s = summarize(records, field="casual_question", k=20, top_n=5)
    text = format_report({150: [s]}, rerank_summaries=[s], k=20, top_n=5)
    assert "Recall@20" in text and "150" in text and "1.00" in text and "폴백" in text and "100%" in text


# --- 캐시 ----------------------------------------------------------------------


# 캐시 파일은 쓰는 즉시 저장되어, 도중에 멈춰도(한도 초과, Ctrl+C) 이미 쓴 호출이 사라지지 않는다.
def test_json_cache_persists_every_write(tmp_path):
    path = tmp_path / "c.json"
    cache = JsonCache(path)
    cache.set("k", {"v": [1, 2]})
    assert JsonCache(path).get("k") == {"v": [1, 2]} and JsonCache(path).get("none") is None


# 깨진 캐시 파일은 조용히 버리지 않고 오류로 알린다(버리면 같은 호출을 다시 해서 무료 한도를 쓴다).
def test_json_cache_refuses_a_corrupt_file(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="캐시"):
        JsonCache(path)


# 같은 질문을 두 번 임베딩하면 진짜 임베더는 한 번만 불린다. 모델·차원이 다르면 다른 칸을 쓴다.
def test_cached_embedder_calls_the_inner_embedder_once(tmp_path):
    inner = FakeEmbedder()
    cache = JsonCache(tmp_path / "e.json")
    embedder = CachedEmbedder(inner, cache, namespace="gemini-embedding-2|768")
    first, second = embedder.embed_query("주휴수당"), embedder.embed_query("주휴수당")
    assert first == second and inner.queries == ["주휴수당"]
    CachedEmbedder(inner, cache, namespace="other-model|768").embed_query("주휴수당")
    assert inner.queries == ["주휴수당", "주휴수당"]


class ScriptedStage:
    """호출 때마다 정해 둔 결과를 돌려주는 리랭크 단계. 불린 횟수를 센다."""

    def __init__(self, reason="ok"):
        self.reason = reason
        self.calls = 0

    def rerank(self, query, hits, top_n=5):
        self.calls += 1
        ordered = list(reversed(hits))[:top_n]  # 일부러 순서를 뒤집어 "리랭크가 일했다"를 보인다
        picked = [dataclasses.replace(h, rank=i, score=round(0.9 - 0.1 * i, 2)) for i, h in enumerate(ordered, start=1)]
        return RerankResult(picked, self.reason == "ok", self.reason)


# 같은 (질문, 후보) 조합은 두 번째부터 캐시에서 나온다: 결과(순서·점수)가 같고 진짜 리랭크는 한 번만 불린다.
def test_cached_rerank_serves_repeated_calls_from_cache(tmp_path):
    inner = ScriptedStage()
    stage = CachedRerankStage(inner, JsonCache(tmp_path / "r.json"), min_interval_s=0.0)
    hits = sample_hits(5)
    first = stage.rerank("q", hits, 3)
    second = stage.rerank("q", hits, 3)
    assert inner.calls == 1 and (stage.live_calls, stage.cache_hits) == (1, 1)
    assert [(h.chunk.chunk_id, h.score) for h in first.hits] == [(h.chunk.chunk_id, h.score) for h in second.hits]
    assert second.used_reranker and second.reason == "ok"


# 후보가 하나라도 다르거나 질문이 다르면 캐시를 쓰지 않는다(옛 결과를 새 질문에 쓰면 측정이 틀린다).
def test_cached_rerank_key_depends_on_query_and_candidates(tmp_path):
    inner = ScriptedStage()
    stage = CachedRerankStage(inner, JsonCache(tmp_path / "r.json"), min_interval_s=0.0)
    hits = sample_hits(5)
    stage.rerank("q", hits, 3)
    stage.rerank("다른 질문", hits, 3)
    stage.rerank("q", hits[:4], 3)
    assert inner.calls == 3


# 한도(cap)·오류(error)로 RRF 순서를 그대로 쓴 결과는 저장하지 않는다 — 다시 실행하면 진짜 리랭크를 다시 시도해야 한다.
@pytest.mark.parametrize("reason", ["cap", "error"])
def test_cached_rerank_does_not_store_fallbacks(tmp_path, reason):
    inner = ScriptedStage(reason=reason)
    stage = CachedRerankStage(inner, JsonCache(tmp_path / "r.json"), min_interval_s=0.0)
    stage.rerank("q", sample_hits(5), 3)
    stage.rerank("q", sample_hits(5), 3)
    assert inner.calls == 2 and stage.cache_hits == 0


# Cohere 시험 키는 분당 10회다. 진짜 호출 사이에는 최소 간격을 두고, 캐시에서 나온 호출은 기다리지 않는다.
def test_cached_rerank_paces_live_calls_but_not_cache_hits(tmp_path):
    now = {"t": 100.0}
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        now["t"] += seconds

    stage = CachedRerankStage(ScriptedStage(), JsonCache(tmp_path / "r.json"), min_interval_s=6.5,
                              clock=lambda: now["t"], sleep=sleep)
    hits = sample_hits(5)
    stage.rerank("첫째", hits, 3)          # 첫 호출은 기다리지 않는다
    now["t"] += 2.0                        # 2초가 지난 뒤
    stage.rerank("둘째", hits, 3)          # 6.5 - 2.0 = 4.5초를 더 기다린다
    stage.rerank("둘째", hits, 3)          # 캐시: 기다리지 않는다
    assert waits == [pytest.approx(4.5)]


# 캐시 파일에 점수가 소수 그대로 남는다(사람이 파일을 열어 확인할 수 있다).
def test_cache_file_is_readable_json(tmp_path):
    path = tmp_path / "r.json"
    CachedRerankStage(ScriptedStage(), JsonCache(path), min_interval_s=0.0).rerank("q", sample_hits(3), 2)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert len(data) == 1 and next(iter(data.values()))["reason"] == "ok"


# --- 명령줄(main) ---------------------------------------------------------------


class DepthStore:
    """깊이를 기억하는 가짜 검색 저장소. 어떤 깊이로 만들어졌는지 확인하려고 쓴다."""

    created: list[int] = []

    def __init__(self, depth):
        self.depth = depth
        DepthStore.created.append(depth)

    def search(self, text, vector, *, size=20, only_current=True):
        return sample_hits(5)[:size]


def write_evalset(path):
    from eval.evalset import save_evalset

    save_evalset(
        # 질문 글이 서로 달라야 리랭크 캐시가 항목을 하나로 합치지 않는다(같은 질문·같은 후보는 일부러 한 번만 부른다).
        [item("a01", {(LAW, "53")}, question="질문 하나", casual="일상어 하나"),
         item("a02", {(LAW, "99")}, question="질문 둘", casual="일상어 둘"),
         item("o01", set(), label="out_of_scope", question="질문 셋", casual="일상어 셋")],
        path,
    )


def run_main(tmp_path, *extra, **parts):
    from eval.run_retrieval import main

    DepthStore.created = []
    path = tmp_path / "set.jsonl"
    write_evalset(path)
    parts.setdefault("embedder", FakeEmbedder())
    parts.setdefault("store_factory", DepthStore)
    return main(["--evalset", str(path), "--cache-dir", str(tmp_path), "--rerank-interval", "0", *extra],
                env={}, **parts)


# 깊이 비교는 요청한 깊이마다 저장소를 한 번씩 만들고, 보고서를 출력하며, --out으로 요약 JSON을 저장한다.
def test_main_compares_depths_prints_report_and_writes_json(tmp_path, capsys):
    out = tmp_path / "out.json"
    embedder = FakeEmbedder()
    assert run_main(tmp_path, "--depths", "100,200", "--out", str(out), embedder=embedder) == 0
    text = capsys.readouterr().out
    assert DepthStore.created == [100, 200] and "Recall@20" in text and "가장 나은 깊이" in text
    assert set(json.loads(out.read_text(encoding="utf-8"))["depths"]) == {"100", "200"}
    # 정답 없는 질문(거절 대상)은 깊이 비교에서 임베딩하지 않는다: 지표 대상이 아닌데 무료 한도만 쓰게 된다.
    # 임베딩은 질문 글마다 한 번만 부른다(깊이 둘이어도 캐시) — 정답 있는 질문 2건 × 문장 2종 = 4번.
    assert sorted(embedder.queries) == sorted(["질문 하나", "일상어 하나", "질문 둘", "일상어 둘"])


# 깊이 비교에는 정답 있는 질문만 쓰고(2건), 리랭크 측정에는 정답 없는 질문도 넣는다(3건). 두 문장 × 항목 수만큼 리랭크한다.
def test_main_rerank_includes_questions_without_gold(tmp_path, capsys):
    stage = FakeRerankStage()
    assert run_main(tmp_path, "--depths", "150", "--rerank", rerank_stage=stage) == 0
    assert stage.calls == 3 * 2  # 항목 3건 × 문장 2종
    assert "리랭크 1위 점수 분포" in capsys.readouterr().out


# 리랭크 깊이를 따로 정하지 않으면 깊이 비교에서 가장 나은 깊이를 쓴다(동점이면 작은 깊이: 이 가짜 저장소는 깊이와 무관하게 같은 결과).
def test_main_reranks_at_the_best_depth_by_default(tmp_path):
    assert run_main(tmp_path, "--depths", "200,100", "--rerank", rerank_stage=FakeRerankStage()) == 0
    assert DepthStore.created == [200, 100, 100]  # 깊이 비교 두 번 + 리랭크 측정 한 번(깊이 100)


# 무료 한도 초과면 종료 코드 2로 멈추고 이어서 하라고 알린다.
def test_main_exits_2_on_quota(tmp_path, capsys):
    class Exhausted(FakeEmbedder):
        def embed_query(self, text):
            raise QuotaExceeded("429", retry_after_s=30.0)

    assert run_main(tmp_path, embedder=Exhausted()) == 2
    assert "이어서" in capsys.readouterr().out


# 키가 없으면 한도를 쓰기 전에 어떤 이름이 없는지 알리고 멈춘다.
def test_main_reports_missing_keys_before_spending_anything(tmp_path, capsys):
    from eval.run_retrieval import main

    path = tmp_path / "set.jsonl"
    write_evalset(path)
    assert main(["--evalset", str(path), "--cache-dir", str(tmp_path)], env={}) == 1
    assert "LABOR_GEMINI_API_KEY" in capsys.readouterr().out


# 평가셋 파일이 없거나 깨졌으면 오류 코드 1.
def test_main_rejects_an_unreadable_evalset(tmp_path, capsys):
    from eval.run_retrieval import main

    assert main(["--evalset", str(tmp_path / "none.jsonl"), "--cache-dir", str(tmp_path)], env={}) == 1
    assert "평가셋" in capsys.readouterr().out
