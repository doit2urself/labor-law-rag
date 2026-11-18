"""검색 평가 지표(eval/metrics.py) 시험 (Task 9).

지표는 순수 함수라 외부 서비스 없이 값만 넣어 확인한다.
"""

import dataclasses

import pytest

from eval.metrics import hit_keys, mrr, recall_at_k
from tests.conftest import sample_hits


# 재현율과 MRR이 계획서가 정한 값(0.5, 1/3, 0.0)과 같은지 확인한다.
def test_recall_and_mrr_values():
    gold = {("근로기준법", "55"), ("근로기준법", "56")}
    ranked = [("근로기준법", "55"), ("민법", "1")]
    assert recall_at_k(gold, ranked, 20) == 0.5
    assert mrr(gold, [("민법", "1"), ("민법", "2"), ("근로기준법", "56")]) == pytest.approx(1 / 3)
    assert mrr(gold, [("민법", "1")]) == 0.0


# 앞 k개만 센다: 21번째에 있는 정답은 Recall@20에는 안 잡히고 Recall@21에는 잡힌다.
def test_recall_counts_only_the_first_k():
    gold = {("근로기준법", "55")}
    ranked = [("민법", str(i)) for i in range(20)] + [("근로기준법", "55")]
    assert recall_at_k(gold, ranked, 20) == 0.0
    assert recall_at_k(gold, ranked, 21) == 1.0


# 같은 정답이 목록에 여러 번 나와도 한 번으로 센다(재현율이 1을 넘으면 안 된다).
def test_recall_never_exceeds_one_when_a_key_repeats():
    gold = {("근로기준법", "55"), ("근로기준법", "56")}
    assert recall_at_k(gold, [("근로기준법", "55")] * 3, 20) == 0.5


# MRR은 처음 맞힌 순위만 본다: 정답이 두 개여도 첫 정답의 순위의 역수다.
def test_mrr_uses_only_the_first_correct_rank():
    gold = {("근로기준법", "55"), ("근로기준법", "56")}
    assert mrr(gold, [("근로기준법", "56"), ("근로기준법", "55")]) == 1.0
    assert mrr(gold, [("민법", "1"), ("근로기준법", "55"), ("근로기준법", "56")]) == 0.5


# 정답이 비어 있으면 0이 아니라 오류다: 정답 없는 질문(거절 대상)을 평균에 섞는 실수를 막는다.
def test_empty_gold_is_rejected_instead_of_scored_zero():
    with pytest.raises(ValueError):
        recall_at_k(set(), [("민법", "1")], 20)
    with pytest.raises(ValueError):
        mrr(set(), [("민법", "1")])


# k는 1 이상이어야 한다(0이면 "아무것도 안 본다"는 뜻이라 지표가 의미 없다).
def test_k_must_be_positive():
    with pytest.raises(ValueError):
        recall_at_k({("근로기준법", "55")}, [("근로기준법", "55")], 0)


# 검색 결과(Hit)를 (법령명, 조문번호) 목록으로 바꾼다. 한 조문이 여러 청크로 나뉘어도 첫 순위 한 번만 남긴다.
def test_hit_keys_dedupes_keeping_first_position():
    base = sample_hits(3)  # 근로기준법 제51, 52, 53조
    split = dataclasses.replace(base[0], chunk=dataclasses.replace(base[0].chunk, chunk_id="x:2", paragraph="2"))
    keys = hit_keys([base[0], base[1], split, base[2]])
    assert keys == [("근로기준법", "51"), ("근로기준법", "52"), ("근로기준법", "53")]


# 법령명이나 조문번호가 없는 청크(정답 키를 만들 수 없는 것)는 목록에서 뺀다.
def test_hit_keys_skips_chunks_without_a_key():
    h = sample_hits(2)
    no_law = dataclasses.replace(h[0], chunk=dataclasses.replace(h[0].chunk, law_name=None))
    no_art = dataclasses.replace(h[1], chunk=dataclasses.replace(h[1].chunk, article_no=None))
    assert hit_keys([no_law, no_art]) == []
