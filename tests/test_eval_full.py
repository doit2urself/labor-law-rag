"""전체 평가(Task 17) 시험: 답변 단계 지표, 검증기 라벨 시트, 리포트, 평가 러너.

진짜 Gemini·Cohere·OpenSearch는 쓰지 않는다. 가짜 부품으로 조립한 진짜 그래프를 돌려서
"질문 하나의 결과를 올바르게 모으는가", "지표 계산이 맞는가", "사람이 아닌 라벨은 거부하는가"를 확인한다.
(진짜 서비스로 돌린 결과는 docs/results.md에 따로 기록한다.)
"""

from __future__ import annotations

import csv
import json

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.domain.errors import ExternalServiceError, QuotaExceeded
from app.graph.build import build_graph
from app.graph.state import checkpoint_serde
from eval.evalset import EvalItem, save_evalset
from eval.labels import (
    export_validator_sheet,
    import_validator_sheet,
    predicted_flags,
    read_validator_sheet,
)
from eval.metrics import (
    AnswerSentence,
    ItemResult,
    answerable_refusal_rate,
    citation_hit_rate,
    compute_metrics,
    latency_percentiles,
    no_verdict_ratio,
    refusal_accuracy,
    validator_accuracy,
)
from eval.report import render_report
from eval.run_eval import (
    load_results,
    main,
    run_item,
    sample_items,
    save_results,
)
from tests.conftest import FakeLlm, deps_with

LAW = "근로기준법"


def res(
    *,
    item_id="a01",
    label="answerable",
    gold=None,
    cited=(),
    refused=False,
    llm_calls=3,
    latency_s=1.0,
    used_reranker=True,
    verdict_found=False,
    reranked_count=5,
    **extra,
) -> ItemResult:
    """시험용 ItemResult. 정답을 안 주면 정답이 필요한 라벨에는 근로기준법 제55조를, 거절 대상에는 빈 정답을 넣는다.

    reranked_count: 리랭크 단계가 남긴 문서 수(기본 5). 0이면 검색·리랭크까지 가지 못한 질문(범위 밖 등)이다.
    """
    if gold is None:
        gold = set() if label in ("no_evidence", "out_of_scope") else {(LAW, "55")}
    return ItemResult(
        reranked_count=reranked_count,
        item_id=item_id,
        label=label,
        gold=frozenset(gold),
        refused=refused,
        cited=frozenset(cited),
        llm_calls=llm_calls,
        latency_s=latency_s,
        used_reranker=used_reranker,
        verdict_found=verdict_found,
        **extra,
    )


# ---- 지표 ------------------------------------------------------------------


# 인용한 출처에 정답 조문이 하나라도 있으면 맞은 것으로 센다(2건 중 1건 → 0.5).
def test_citation_hit_rate():
    rs = [res(cited={(LAW, "55")}, gold={(LAW, "55")}), res(cited={("민법", "1")}, gold={(LAW, "55")})]
    assert citation_hit_rate(rs) == 0.5


# 정답이 여러 개일 때 하나만 인용해도 맞다. 답할 수 있는 질문을 거절한 것은 틀린 것으로 센다. 다른 라벨은 분모에 넣지 않는다.
def test_citation_hit_rate_counts_refused_answerable_as_a_miss_and_ignores_other_labels():
    rs = [
        res(item_id="a01", cited={(LAW, "56")}, gold={(LAW, "55"), (LAW, "56")}),
        res(item_id="a02", refused=True, cited=set()),
        res(item_id="v01", label="verdict", cited={(LAW, "55")}),
        res(item_id="o01", label="out_of_scope", refused=True),
    ]
    assert citation_hit_rate(rs) == 0.5


# 답할 수 있는 질문이 하나도 없으면 0점으로 속이지 않고 오류를 낸다(평균이 조용히 깎이는 것을 막는 이 프로젝트의 규칙).
def test_citation_hit_rate_without_answerable_items_raises():
    with pytest.raises(ValueError):
        citation_hit_rate([res(label="out_of_scope", refused=True)])


# 거절해야 하는 라벨(범위 밖, 근거 없음)만 분모에 넣는다: 2건 중 1건 거절 → 0.5.
def test_refusal_accuracy_only_counts_negative_items():
    rs = [res(label="out_of_scope", refused=True), res(label="no_evidence", refused=False), res(label="answerable", refused=False)]
    assert refusal_accuracy(rs) == 0.5


# 답할 수 있는 질문을 잘못 거절한 비율: 사용자가 가장 먼저 체감하는 실패라서 따로 센다.
def test_answerable_refusal_rate():
    rs = [res(refused=True), res(refused=False), res(refused=False), res(label="out_of_scope", refused=True)]
    assert answerable_refusal_rate(rs) == pytest.approx(1 / 3)


# 거절한 질문은 "답변"이 없으므로 결론 문장 비율의 분모에 넣지 않는다(답변 3건 중 1건에 결론 문장).
def test_no_verdict_ratio_counts_only_answered_items():
    rs = [res(verdict_found=False), res(verdict_found=True), res(verdict_found=False), res(refused=True, verdict_found=True)]
    assert no_verdict_ratio(rs) == pytest.approx(2 / 3)


# 거절한 답변만 있으면 비율을 낼 수 없다.
def test_no_verdict_ratio_without_answers_raises():
    with pytest.raises(ValueError):
        no_verdict_ratio([res(refused=True)])


# 검증기와 사람의 판정이 같은 비율. 길이가 다르거나 비었으면 계산하지 않는다.
def test_validator_accuracy():
    assert validator_accuracy([True, False, True, True], [True, True, True, False]) == 0.5
    with pytest.raises(ValueError):
        validator_accuracy([True], [True, False])
    with pytest.raises(ValueError):
        validator_accuracy([], [])


# numpy 기본 정의(선형 보간)와 같아야 한다: 95번째 백분위 = 4 + 0.8 × (100 − 4) = 80.8.
def test_latency_percentiles():
    assert latency_percentiles([1.0, 2.0, 3.0, 4.0, 100.0]) == (3.0, pytest.approx(80.8, rel=0.01))


# 값이 하나뿐이면 p50·p95가 그 값이고, 비었으면 계산하지 않는다. 입력 순서와 상관없다.
def test_latency_percentiles_edge_cases():
    assert latency_percentiles([2.5]) == (2.5, 2.5)
    assert latency_percentiles([100.0, 1.0, 3.0, 4.0, 2.0])[0] == 3.0
    with pytest.raises(ValueError):
        latency_percentiles([])


# 지표를 한 번에 모은다. 분모가 0인 지표는 0이 아니라 None(측정 불가)으로 표시한다.
def test_compute_metrics_reports_none_when_there_is_nothing_to_measure():
    only_refusals = [res(label="out_of_scope", refused=True, used_reranker=False, reranked_count=0, llm_calls=1, latency_s=0.5)]
    m = compute_metrics(only_refusals)
    assert m["n_items"] == 1
    assert m["citation_hit_rate"] is None and m["no_verdict_ratio"] is None and m["answerable_refusal_rate"] is None
    assert m["rerank_fallback_rate"] is None  # 리랭크 단계까지 간 질문이 없다
    assert m["refusal_accuracy"] == 1.0


# 모은 값: 질문당 LLM 호출 평균·최대, 리랭크 폴백 비율(리랭크 단계까지 간 질문만 분모), 다시 쓰기·다시 찾기 비율, 지연 시간.
def test_compute_metrics_values():
    rs = [
        res(item_id="a01", cited={(LAW, "55")}, llm_calls=3, latency_s=2.0),
        res(item_id="a02", cited={("민법", "1")}, llm_calls=5, latency_s=4.0, used_reranker=False, regen_count=1),
        res(item_id="n01", label="no_evidence", refused=True, llm_calls=1, latency_s=1.0, research_count=2,
            used_reranker=False, reranked_count=0),
    ]
    m = compute_metrics(rs)
    assert m["n_items"] == 3 and m["citation_hit_rate"] == 0.5
    assert (m["llm_calls_mean"], m["llm_calls_max"]) == (3.0, 5)
    assert m["rerank_fallback_rate"] == 0.5  # 리랭크까지 간 2건 중 1건이 폴백(n01은 거기까지 가지 않았다)
    assert m["regen_rate"] == pytest.approx(1 / 3) and m["research_rate"] == pytest.approx(1 / 3)
    assert m["latency_p50_s"] == 2.0


# ---- 검증기 라벨 시트 -----------------------------------------------------------


def answered(item_id, *, sentences, sources=None, question="질문") -> ItemResult:
    """문장과 출처를 가진 답변 결과. sentences: [(글, 인용 ID 목록, 검증기 판정)]."""
    return res(
        item_id=item_id,
        question=question,
        sentences=tuple(AnswerSentence(t, tuple(c), ok) for t, c, ok in sentences),
        sources=sources if sources is not None else {"doc1": "근로기준법 제55조(휴일): 사용자는 유급휴일을 보장하여야 한다."},
    )


def fill_sheet(path, *, labeler, marks):
    """시트의 일치 칸과 검수자 칸을 채워 다시 저장한다(사람이 하는 일을 흉내 낸다). marks: 줄 순서대로 "O"/"X"/""."""
    with open(path, encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    for row, mark in zip(rows, marks):
        row["일치(O/X)"] = mark
        row["검수자"] = labeler if mark else ""
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def sheet_with_labeler(tmp_path, labeler):
    results = [answered("a01", sentences=[("문장 하나", ["doc1"], True)])]
    path = tmp_path / "validator.csv"
    export_validator_sheet(results, path, n=5)
    fill_sheet(path, labeler=labeler, marks=["O"])
    return path


# 질문을 만든 모델이 스스로 라벨을 달면 검증기 정확도가 부풀려진다. 사람이 아닌 검수자는 거부한다.
@pytest.mark.parametrize("labeler", ["llm", "Claude", "GEMINI"])
def test_validator_sheet_rejects_llm_labeler(tmp_path, labeler):
    with pytest.raises(ValueError):
        import_validator_sheet(sheet_with_labeler(tmp_path, labeler))


# 인용이 없는 문장은 "인용 문서로 뒷받침되는가"를 물을 수 없으니 시트에 넣지 않고, 검증기의 판정은 사람에게 보이지 않는다.
def test_export_validator_sheet_lists_only_cited_sentences_and_hides_the_verifier_verdict(tmp_path):
    results = [answered("a01", sentences=[("인용 문장", ["doc1"], True), ("서두 문장", [], True)], question="알바도 주휴수당 받나요?")]
    path = tmp_path / "validator.csv"
    export_validator_sheet(results, path, n=10)
    with open(path, encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert list(rows[0].keys()) == ["id", "질문", "답변문장", "인용문서", "일치(O/X)", "검수자"]
    assert len(rows) == 1
    assert rows[0]["id"] == "a01#1" and rows[0]["질문"] == "알바도 주휴수당 받나요?" and rows[0]["답변문장"] == "인용 문장"
    assert "유급휴일" in rows[0]["인용문서"] and rows[0]["일치(O/X)"] == "" and rows[0]["검수자"] == ""


# 문장이 n개보다 많으면 n개만 뽑되, 같은 시드면 항상 같은 문장이고 시트 순서는 결과 순서를 따른다.
def test_export_validator_sheet_samples_deterministically(tmp_path):
    results = [answered(f"a{i:02d}", sentences=[("문장", ["doc1"], True)]) for i in range(1, 11)]
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    export_validator_sheet(results, first, n=4)
    export_validator_sheet(results, second, n=4)
    assert first.read_bytes() == second.read_bytes()
    ids = [row["id"] for row in csv.DictReader(open(first, encoding="utf-8-sig", newline=""))]
    assert len(ids) == 4 and ids == sorted(ids)


# 사람이 채운 시트를 덮어쓰면 안 된다.
def test_export_validator_sheet_refuses_to_overwrite(tmp_path):
    results = [answered("a01", sentences=[("문장", ["doc1"], True)])]
    path = tmp_path / "validator.csv"
    export_validator_sheet(results, path, n=1)
    with pytest.raises(FileExistsError):
        export_validator_sheet(results, path, n=1)


# 사람이 O/X를 채운 줄만 가져온다(안 채운 줄은 건너뜀). 가져온 값은 줄 순서대로 True/False다.
def test_import_validator_sheet_reads_marks_and_skips_unlabeled_rows(tmp_path):
    results = [answered(f"a{i:02d}", sentences=[("문장", ["doc1"], True)]) for i in range(1, 5)]
    path = tmp_path / "validator.csv"
    export_validator_sheet(results, path, n=4)
    fill_sheet(path, labeler="홍길동", marks=["O", "X", "", "o"])
    assert import_validator_sheet(path) == [True, False, True]
    assert list(read_validator_sheet(path)) == ["a01#1", "a02#1", "a04#1"]


# 검수자만 있거나 일치 칸만 있거나 O/X가 아닌 값이면 오류. 앞뒤가 안 맞는 시트를 조용히 받아들이지 않는다.
@pytest.mark.parametrize("labeler, mark", [("", "O"), ("홍길동", ""), ("홍길동", "맞음")])
def test_import_validator_sheet_rejects_inconsistent_rows(tmp_path, labeler, mark):
    results = [answered("a01", sentences=[("문장", ["doc1"], True)])]
    path = tmp_path / "validator.csv"
    export_validator_sheet(results, path, n=1)
    with open(path, encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    rows[0]["일치(O/X)"], rows[0]["검수자"] = mark, labeler
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError):
        import_validator_sheet(path)


# 검증기의 판정을 사람이 채운 줄의 id 순서에 맞춰 꺼낸다 → validator_accuracy의 입력이 된다.
def test_predicted_flags_follow_the_labeled_keys():
    results = [
        answered("a01", sentences=[("가", ["doc1"], True)]),
        answered("a02", sentences=[("나", ["doc1"], False), ("다", ["doc1"], True)]),
    ]
    assert predicted_flags(results, ["a02#2", "a01#1", "a02#1"]) == [True, True, False]
    with pytest.raises(KeyError):
        predicted_flags(results, ["a09#1"])


# ---- 리포트 ----------------------------------------------------------------


# 사람이 확인한 건수를 반드시 적는다(없으면 평가가 자기 점수를 스스로 올린다는 의심을 받는다).
def test_report_states_human_checked_count():
    md = render_report({"recall@20": 0.8, "mrr": 0.5}, human_checked=80)
    assert "사람이 확인한 건수: 80" in md and "0.8" in md


# 사람이 확인한 것이 없어도 0이라고 적는다. 측정하지 못한 지표는 0이 아니라 "측정 불가"로, 덧붙인 주의사항은 그대로 보인다.
def test_report_marks_unmeasured_metrics_and_lists_notes():
    md = render_report({"citation_hit_rate": None, "n_items": 12}, human_checked=0, notes=["초안 평가셋이다"])
    assert "사람이 확인한 건수: 0" in md
    assert "측정 불가" in md and "초안 평가셋이다" in md and "12" in md


# ---- 러너 ------------------------------------------------------------------


def make_graph(llm, **overrides):
    """가짜 부품으로 조립한 진짜 그래프(서버와 같은 build_graph). 대화 상태는 메모리에만 둔다."""
    return build_graph(deps_with(llm, **overrides), InMemorySaver(serde=checkpoint_serde()))


def item(item_id="a01", label="answerable", gold=((LAW, "51"),), casual="알바도 주휴수당 받나요?", reviewer=None) -> EvalItem:
    return EvalItem(item_id, "조문 어휘형 질문", casual, frozenset(gold), "요약", label, reviewer)


# 정상 답변 한 건에서 모으는 값: 인용한 조문 키, LLM 호출 수(분석+생성+검증=3), 리랭크 사용, 문장별 검증기 판정, 인용 출처 글.
def test_run_item_collects_the_answer_facts_from_the_real_graph():
    graph = make_graph(FakeLlm.script(qclass="counsel"))
    r = run_item(graph, item(), "알바도 주휴수당 받나요?")
    assert (r.item_id, r.label, r.refused) == ("a01", "answerable", False)
    assert r.cited == {(LAW, "51")} and r.gold == {(LAW, "51")}
    assert (r.llm_calls, r.used_reranker, r.verdict_found, r.regen_count, r.research_count) == (3, True, False, 0, 0)
    assert r.question == "알바도 주휴수당 받나요?"
    assert [(s.text, s.citation_ids, s.verifier_ok) for s in r.sentences] == [("주휴수당은 유급휴일에 대한 임금입니다.", ("doc1",), True)]
    assert "제51조" in r.sources["doc1"]


# 거절한 질문은 인용·문장이 없고, 분석 한 번(LLM 1회)만 쓴다.
def test_run_item_records_a_refusal():
    graph = make_graph(FakeLlm.script(qclass="out_of_scope"))
    r = run_item(graph, item("o01", "out_of_scope", gold=()), "전세 보증금 돌려받는 법")
    assert r.refused is True and r.cited == frozenset() and r.sentences == ()
    assert (r.llm_calls, r.verdict_found) == (1, False)


# 검증기가 첫 답변에서 사건 판단 문장을 찾으면 같은 근거로 답변을 다시 쓴다: regen_count 1, 호출 5회(분석·생성·검증·생성·검증).
# 나간 답변의 verdict_found는 거짓이다(결론 문장이 있는 답변은 나가지 못한다).
def test_run_item_counts_regeneration_after_a_verdict_sentence():
    graph = make_graph(FakeLlm.script(qclass="counsel", verdict_found_once=True))
    r = run_item(graph, item(), "질문")
    assert (r.refused, r.regen_count, r.research_count, r.llm_calls, r.verdict_found) == (False, 1, 0, 5, False)


# 검증기가 모든 근거 문장을 "불일치"로 보면 문서가 부족했다고 보고 검색어를 다시 만들어 다시 찾는다:
# research_count 1, 호출 6회(분석·생성·검증·검색어 다시 만들기·생성·검증).
def test_run_item_counts_research_after_unsupported_sentences():
    graph = make_graph(FakeLlm.script(qclass="counsel", verification_fails_once=True))
    r = run_item(graph, item(), "질문")
    assert (r.refused, r.regen_count, r.research_count, r.llm_calls) == (False, 0, 1, 6)


# 지연 시간에서 인위적인 대기(Cohere 분당 한도 때문에 일부러 쉬는 시간)를 뺀다. 안 빼면 p95가 실제 서비스보다 훨씬 나빠 보인다.
def test_run_item_subtracts_deliberate_waiting_from_latency():
    clock_values = iter([100.0, 110.0])
    slept_values = iter([0.0, 4.0])
    graph = make_graph(FakeLlm.script(qclass="counsel"))
    r = run_item(graph, item(), "질문", clock=lambda: next(clock_values), slept=lambda: next(slept_values))
    assert r.latency_s == 6.0


# 같은 대화 번호를 재사용하지 않는다: 항목마다 새 대화라서 앞 항목의 호출 수가 이어지지 않는다.
def test_run_item_uses_a_fresh_conversation_each_time():
    graph = make_graph(FakeLlm.script(qclass="counsel"))
    first = run_item(graph, item("a01"), "질문 하나")
    second = run_item(graph, item("a02"), "질문 둘")
    assert first.llm_calls == second.llm_calls == 3


# --limit: 라벨별로 고르게 N건을 고른다(앞에서 N건만 자르면 답할 수 있는 질문만 남아 거절 지표를 못 낸다). 항상 같은 결과.
def test_sample_items_is_even_deterministic_and_covers_labels():
    items = (
        [item(f"a{i:02d}") for i in range(1, 31)]
        + [item(f"v{i:02d}", "verdict") for i in range(1, 5)]
        + [item(f"n{i:02d}", "no_evidence", gold=()) for i in range(1, 4)]
        + [item(f"o{i:02d}", "out_of_scope", gold=()) for i in range(1, 4)]
    )
    picked = sample_items(items, 15)
    assert [i.id for i in picked] == [i.id for i in sample_items(items, 15)]
    assert len(picked) == 15 and len({i.id for i in picked}) == 15
    assert {i.label for i in picked} == {"answerable", "verdict", "no_evidence", "out_of_scope"}
    assert [i.id for i in sample_items(items, 100)] == [i.id for i in items]  # 전체보다 크면 전부
    with pytest.raises(ValueError):
        sample_items(items, 0)


# 결과 파일을 저장했다가 읽으면 같은 값이 나온다(정답·인용 집합, 문장, 출처까지). 평가 도중 멈춘 뒤 이어서 하려면 필요하다.
def test_results_roundtrip_through_json(tmp_path):
    original = answered("a01", sentences=[("문장", ["doc1", "doc2"], False)], sources={"doc1": "가", "doc2": "나"})
    original = ItemResult(**{**original.__dict__, "cited": frozenset({(LAW, "55"), (LAW, "56의2")}), "regen_count": 2})
    path = tmp_path / "results.json"
    save_results(path, [original], {"model": "m", "field": "casual_question"})
    meta, loaded = load_results(path)
    assert meta == {"model": "m", "field": "casual_question"}
    assert loaded == [original]
    assert json.loads(path.read_text(encoding="utf-8"))["results"][0]["item_id"] == "a01"  # 사람이 읽을 수 있는 JSON


# ---- main(명령줄) ----------------------------------------------------------


def write_evalset(tmp_path, items):
    path = tmp_path / "evalset.jsonl"
    save_evalset(items, path)
    return str(path)


def base_args(tmp_path, evalset, *extra):
    return ["--evalset", evalset, "--out", str(tmp_path / "results.json"), "--cache-dir", str(tmp_path / "cache"), *extra]


# 평가셋 전체를 돌려 결과 JSON과 리포트를 쓴다. 사람이 확인한 건수는 평가셋의 reviewer로 센다.
def test_main_runs_every_item_and_writes_results_and_report(tmp_path, capsys):
    evalset = write_evalset(tmp_path, [item("a01", reviewer="홍길동"), item("a02"), item("a03")])
    report = tmp_path / "results.md"
    code = main(base_args(tmp_path, evalset, "--report", str(report)), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    assert code == 0
    meta, results = load_results(tmp_path / "results.json")
    assert [r.item_id for r in results] == ["a01", "a02", "a03"] and meta["field"] == "casual_question"
    text = report.read_text(encoding="utf-8")
    assert "사람이 확인한 건수: 1" in text and "casual_question" in text
    assert "질문당 LLM 호출" in capsys.readouterr().out


# 일상어 질문이 없는 항목(검수에서 반려됨)은 그 문장 종류로는 건너뛴다: 다른 문장으로 대신하면 점수가 섞인다.
def test_main_skips_items_without_the_selected_phrasing(tmp_path):
    evalset = write_evalset(tmp_path, [item("a01"), item("a02", casual=None)])
    main(base_args(tmp_path, evalset), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    _, results = load_results(tmp_path / "results.json")
    assert [r.item_id for r in results] == ["a01"]


# --field question이면 조문 어휘형 질문으로 돌린다.
def test_main_field_option_selects_the_article_phrasing(tmp_path):
    llm = FakeLlm.script(qclass="counsel")
    evalset = write_evalset(tmp_path, [item("a01")])
    main(base_args(tmp_path, evalset, "--field", "question"), env={}, graph=make_graph(llm))
    assert "조문 어휘형 질문" in llm.prompts[0].user


# --limit은 라벨별로 고르게 N건만 돌린다.
def test_main_limit_runs_a_spread_sample(tmp_path):
    items = [item(f"a{i:02d}") for i in range(1, 7)] + [item("n01", "no_evidence", gold=())]
    evalset = write_evalset(tmp_path, items)
    main(base_args(tmp_path, evalset, "--limit", "3"), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    _, results = load_results(tmp_path / "results.json")
    assert len(results) == 3 and "n01" in {r.item_id for r in results}


# 이미 결과 파일이 있으면 덮어쓰지 않고 멈춘다: 모델·프롬프트가 바뀐 줄 모르고 옛 결과가 섞이는 사고를 막는다.
def test_main_refuses_to_reuse_an_existing_results_file(tmp_path, capsys):
    evalset = write_evalset(tmp_path, [item("a01")])
    (tmp_path / "results.json").write_text("{}", encoding="utf-8")
    code = main(base_args(tmp_path, evalset), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    assert code == 1 and "--resume" in capsys.readouterr().out
    assert (tmp_path / "results.json").read_text(encoding="utf-8") == "{}"


class QuotaAfter:
    """진짜 그래프를 감싸서, 정해진 횟수만큼 실행한 뒤에는 무료 한도 초과를 내는 가짜(한도가 도중에 닿는 상황 시험용)."""

    def __init__(self, graph, ok_runs, error=None):
        self._graph, self._left = graph, ok_runs
        self._error = error or QuotaExceeded("한도", retry_after_s=30)
        self.runs = 0

    def stream(self, *args, **kwargs):
        self.runs += 1
        if self._left <= 0:
            raise self._error
        self._left -= 1
        yield from self._graph.stream(*args, **kwargs)

    def get_state(self, *args, **kwargs):
        return self._graph.get_state(*args, **kwargs)


# 한도 초과가 나면 지금까지의 결과를 저장하고 종료 코드 2로 멈춘다. --resume으로 이어서 하면 끝난 항목은 다시 돌리지 않는다.
def test_main_stops_on_quota_keeps_partial_results_and_resumes(tmp_path, capsys):
    evalset = write_evalset(tmp_path, [item("a01"), item("a02"), item("a03")])
    flaky = QuotaAfter(make_graph(FakeLlm.script(qclass="counsel")), ok_runs=1)
    assert main(base_args(tmp_path, evalset), env={}, graph=flaky) == 2
    assert "--resume" in capsys.readouterr().out
    _, partial = load_results(tmp_path / "results.json")
    assert [r.item_id for r in partial] == ["a01"]

    healthy = QuotaAfter(make_graph(FakeLlm.script(qclass="counsel")), ok_runs=10)
    assert main(base_args(tmp_path, evalset, "--resume"), env={}, graph=healthy) == 0
    assert healthy.runs == 2  # a01은 다시 돌리지 않았다
    _, done = load_results(tmp_path / "results.json")
    assert [r.item_id for r in done] == ["a01", "a02", "a03"]


# 한도 초과가 아닌 외부 서비스 오류도 같은 방식으로 멈춘다(질문 하나를 "실패 결과"로 섞어 지표를 흐리지 않는다).
def test_main_stops_on_other_external_errors_too(tmp_path):
    evalset = write_evalset(tmp_path, [item("a01"), item("a02")])
    flaky = QuotaAfter(make_graph(FakeLlm.script(qclass="counsel")), ok_runs=1, error=ExternalServiceError("시간 초과"))
    assert main(base_args(tmp_path, evalset), env={}, graph=flaky) == 2


# 설정이 다른 결과 파일을 이어 받으면 지표가 섞인다: 모델이나 문장 종류가 다르면 멈춘다.
def test_main_resume_refuses_a_results_file_from_another_setup(tmp_path, capsys):
    evalset = write_evalset(tmp_path, [item("a01"), item("a02")])
    main(base_args(tmp_path, evalset, "--model", "model-a"), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    code = main(base_args(tmp_path, evalset, "--resume", "--model", "model-b"), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    assert code == 1 and "model-a" in capsys.readouterr().out


# 키가 없으면 무료 한도를 쓰기 전에 알리고 멈춘다(진짜 그래프를 만들어야 하는데 graph를 안 준 경우).
def test_main_reports_missing_keys_before_spending_anything(tmp_path, capsys):
    evalset = write_evalset(tmp_path, [item("a01")])
    assert main(base_args(tmp_path, evalset), env={}) == 1
    out = capsys.readouterr().out
    assert "LABOR_GEMINI_API_KEY" in out and "LABOR_COHERE_API_KEY" in out and "LABOR_OPENSEARCH_PASSWORD" in out


# 읽을 수 없는 평가셋이면 오류로 멈춘다.
def test_main_rejects_an_unreadable_evalset(tmp_path, capsys):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{not json", encoding="utf-8")
    assert main(base_args(tmp_path, str(bad)), env={}, graph=make_graph(FakeLlm.script(qclass="counsel"))) == 1
    assert "평가셋" in capsys.readouterr().out


# 저장된 결과에서 검증기 라벨 시트를 만들고(평가를 다시 돌리지 않는다), 사람이 채운 시트를 읽어 검증기 정확도를 리포트에 넣는다.
# 사람이 확인한 건수에는 검증기를 채점한 문장 수가 더해진다.
def test_main_exports_the_validator_sheet_and_scores_it(tmp_path):
    evalset = write_evalset(tmp_path, [item("a01"), item("a02"), item("a03")])
    main(base_args(tmp_path, evalset), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    sheet = tmp_path / "validator.csv"
    never_called = QuotaAfter(make_graph(FakeLlm.script(qclass="counsel")), ok_runs=0)
    assert main(base_args(tmp_path, evalset, "--resume", "--export-validator", str(sheet)), env={}, graph=never_called) == 0
    assert never_called.runs == 0 and sheet.exists()

    fill_sheet(sheet, labeler="홍길동", marks=["O", "X", "O"])  # 검증기는 3문장 모두 "일치"라고 했으니 정확도 2/3
    report = tmp_path / "results.md"
    code = main(base_args(tmp_path, evalset, "--resume", "--validator-sheet", str(sheet), "--report", str(report)),
                env={}, graph=never_called)
    assert code == 0
    text = report.read_text(encoding="utf-8")
    assert "사람이 확인한 건수: 3" in text and "0.67" in text


# 이미 있는 시트는 덮어쓰지 않는다(사람이 채운 시트일 수 있다). 오류로 알리고 멈춘다.
def test_main_refuses_to_overwrite_an_existing_validator_sheet(tmp_path, capsys):
    evalset = write_evalset(tmp_path, [item("a01")])
    main(base_args(tmp_path, evalset), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    sheet = tmp_path / "validator.csv"
    sheet.write_text("사람이 고친 시트", encoding="utf-8")
    code = main(base_args(tmp_path, evalset, "--resume", "--export-validator", str(sheet)), env={}, graph=make_graph(FakeLlm.script()))
    assert code == 1 and sheet.read_text(encoding="utf-8") == "사람이 고친 시트"
    assert "덮어쓰지" in capsys.readouterr().out


# 다른 실행에서 나온 시트(결과에 없는 id)를 가져오면 조용히 넘기지 않고 오류로 알린다.
def test_main_rejects_a_validator_sheet_from_another_run(tmp_path, capsys):
    evalset = write_evalset(tmp_path, [item("a01")])
    main(base_args(tmp_path, evalset), env={}, graph=make_graph(FakeLlm.script(qclass="counsel")))
    sheet = tmp_path / "validator.csv"
    sheet.write_text("id,질문,답변문장,인용문서,일치(O/X),검수자\nz99#1,q,s,d,O,홍길동\n", encoding="utf-8-sig")
    code = main(base_args(tmp_path, evalset, "--resume", "--validator-sheet", str(sheet)), env={}, graph=make_graph(FakeLlm.script()))
    assert code == 1 and "z99#1" in capsys.readouterr().out


# 문장이 문서 두 개를 인용하면 인용문서 칸에 줄바꿈이 들어간다. 시트를 저장했다 읽어도 칸의 글이 그대로 보존되어야 한다
# (줄바꿈이 사라져 글이 이어 붙으면 사람이 채점할 때 보는 것과 다른 글이 된다).
def test_validator_sheet_survives_multiline_cells(tmp_path):
    from eval.labels import _read_rows

    sources = {"doc1": "근로기준법 제55조: 첫째 줄\n둘째 줄", "doc2": "근로기준법 제56조: 연장근로"}
    results = [answered("a01", sentences=[("두 문서를 인용한 문장", ["doc1", "doc2"], True)], sources=sources)]
    path = tmp_path / "validator.csv"
    export_validator_sheet(results, path, n=5)
    fill_sheet(path, labeler="홍길동", marks=["X"])
    assert read_validator_sheet(path) == {"a01#1": False}
    expected = "근로기준법 제55조: 첫째 줄\n둘째 줄\n\n근로기준법 제56조: 연장근로"
    assert _read_rows(path, ["id", "인용문서"])[0]["인용문서"] == expected


# 나간 문장이 생성된 문장보다 하나 적으면(서두 문장을 화면용 값이 뺐다) 앞의 판정 하나를 버려 위치를 맞춘다. 맞출 수 없으면 None.
def test_verifier_flags_align_with_the_sentences_that_were_shown():
    from types import SimpleNamespace

    from eval.run_eval import _verifier_flags

    def values(sentence_count, flags):
        return {
            "answer": SimpleNamespace(sentences=[object()] * sentence_count),
            "verification": SimpleNamespace(sentences_ok=flags),
        }

    assert _verifier_flags(values(3, [False, True, False]), 3) == [False, True, False]
    assert _verifier_flags(values(3, [False, True, False]), 2) == [True, False]   # 서두 문장이 빠진 경우
    assert _verifier_flags(values(3, [False, True, False]), 1) == [None]          # 2개 이상 차이 → 맞출 수 없다
    assert _verifier_flags(values(3, [True, True]), 3) == [None, None, None]      # 판정 개수가 문장 수와 다르다
    assert _verifier_flags({"answer": None, "verification": None}, 2) == [None, None]


# 출처의 조문번호와 가지번호를 정답과 같은 "56의2" 모양으로 합친다. 법령명이나 조문번호가 없으면 키를 만들지 않는다.
def test_source_key_joins_branch_numbers_like_the_gold_keys():
    from eval.run_eval import source_key

    assert source_key({"law_name": LAW, "article_no": "56", "branch_no": "2"}) == (LAW, "56의2")
    assert source_key({"law_name": LAW, "article_no": "55", "branch_no": None}) == (LAW, "55")
    assert source_key({"law_name": None, "article_no": "55", "branch_no": None}) is None
    assert source_key({"law_name": LAW, "article_no": None, "branch_no": None}) is None


# 리랭크가 한도·오류로 폴백하면 used_reranker가 거짓이고, 리랭크 단계까지는 갔으니 reranked_count는 남는다(폴백 비율의 분모에 든다).
def test_run_item_reports_a_reranker_fallback():
    from tests.conftest import FakeRerankStage

    graph = make_graph(FakeLlm.script(qclass="counsel"), rerank=FakeRerankStage(used_reranker=False, reason="error"))
    r = run_item(graph, item(), "질문")
    assert r.used_reranker is False and r.reranked_count == 5
    assert compute_metrics([r])["rerank_fallback_rate"] == 1.0
