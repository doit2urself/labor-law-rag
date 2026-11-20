"""평가셋 모델·저장·검증(eval/evalset.py) 시험 (Task 9).

계획서 개정 1: 평가셋은 해석에서 뽑지 않고 LLM이 초안을 쓴다(eval/drafts.py). 그래서 이 파일은 "저장·검증 규칙"과
"실제로 커밋된 평가셋이 규칙을 지키는가"를 시험한다.
"""

from pathlib import Path

import pytest

from app.scope import COVERED_LAWS
from eval.evalset import (
    EvalItem,
    check_gold_exists,
    label_shares,
    load_evalset,
    save_evalset,
    validate_items,
)

LAW = "근로기준법"


def make_item(item_id: str = "a01", **overrides) -> EvalItem:
    """시험용 항목. 기본값은 정답이 있는 answerable 항목이다."""
    values = dict(
        id=item_id,
        question="1주 동안 소정근로일을 개근한 근로자에게 유급휴일을 주어야 하나요?",
        casual_question="알바 주휴수당이 뭐예요?",
        gold=frozenset({(LAW, "55")}),
        gold_answer="1주에 평균 1회 이상 유급휴일을 보장한다.",
        label="answerable",
    )
    values.update(overrides)
    return EvalItem(**values)


# 저장했다가 읽으면 같은 항목이 나온다(한글이 \\u 이스케이프로 깨지지 않고, 정답 순서는 항상 같다).
def test_save_then_load_round_trips_and_keeps_korean_readable(tmp_path):
    items = [
        make_item("a01", gold=frozenset({(LAW, "56"), (LAW, "55")}), reviewer="홍길동"),
        make_item("o01", label="out_of_scope", gold=frozenset(), casual_question=None),
    ]
    path = tmp_path / "set.jsonl"
    save_evalset(items, path)
    assert load_evalset(path) == items
    raw = path.read_bytes()
    assert "근로기준법".encode("utf-8") in raw and b"\r\n" not in raw  # 한글 그대로, 줄바꿈은 LF
    assert raw.index("55".encode()) < raw.index("56".encode())  # 정답은 정렬해서 쓴다(파일이 커밋돼도 diff가 안정적)


# 같은 항목을 두 번 저장한 파일은 바이트까지 같다(사람이 diff로 검수 변경만 볼 수 있어야 한다).
def test_save_is_deterministic(tmp_path):
    items = [make_item("a01", gold=frozenset({(LAW, "56"), (LAW, "55"), ("최저임금법", "5")}))]
    save_evalset(items, tmp_path / "1.jsonl")
    save_evalset(items, tmp_path / "2.jsonl")
    assert (tmp_path / "1.jsonl").read_bytes() == (tmp_path / "2.jsonl").read_bytes()


# 알 수 없는 라벨은 읽을 때 줄 번호와 함께 거절한다.
def test_load_rejects_unknown_label_with_line_number(tmp_path):
    path = tmp_path / "bad.jsonl"
    save_evalset([make_item("a01")], path)
    path.write_text(path.read_text(encoding="utf-8").replace('"answerable"', '"mystery"'), encoding="utf-8")
    with pytest.raises(ValueError, match="1번째 줄.*mystery"):
        load_evalset(path)


# 규칙 검사: 정답이 필요한 라벨에 정답이 없으면 오류.
def test_validate_requires_gold_for_answerable_and_verdict():
    for label in ("answerable", "verdict"):
        problems = validate_items([make_item(label=label, gold=frozenset())], set(COVERED_LAWS))
        assert any("정답" in p for p in problems)


# 규칙 검사: 거절 대상(no_evidence, out_of_scope)에 정답이 있으면 오류 — 있다면 거절하면 안 되는 질문이다.
def test_validate_forbids_gold_for_refusal_labels():
    for label in ("no_evidence", "out_of_scope"):
        problems = validate_items([make_item(label=label, gold=frozenset({(LAW, "55")}))], set(COVERED_LAWS))
        assert any("정답" in p for p in problems)


# 규칙 검사: 수집 범위 밖 법령이 정답에 있으면 오류(검색 지표가 구조적으로 0이 된다, 스펙 6.2).
def test_validate_rejects_gold_outside_covered_laws():
    problems = validate_items([make_item(gold=frozenset({("민법", "1")}))], set(COVERED_LAWS))
    assert any("민법" in p for p in problems)


# 규칙 검사: id 중복과 빈 질문은 오류.
def test_validate_rejects_duplicate_ids_and_blank_questions():
    problems = validate_items([make_item("a01"), make_item("a01", question="  ")], set(COVERED_LAWS))
    assert any("중복" in p for p in problems) and any("질문" in p for p in problems)


# 정상 항목은 문제 없음.
def test_validate_accepts_a_good_item():
    assert validate_items([make_item()], set(COVERED_LAWS)) == []


# 정답 조문이 실제 색인 대상(파서가 읽은 청크)에 있는지 확인한다: 없는 조문을 정답으로 적으면 영원히 못 맞힌다.
def test_check_gold_exists_reports_missing_articles():
    known = {(LAW, "55")}
    problems = check_gold_exists([make_item(gold=frozenset({(LAW, "55"), (LAW, "9999")}))], known)
    assert len(problems) == 1 and "9999" in problems[0]


# 라벨별 비율: 분모는 전체 항목 수.
def test_label_shares():
    items = [make_item("a1"), make_item("a2"), make_item("v1", label="verdict"),
             make_item("o1", label="out_of_scope", gold=frozenset())]
    assert label_shares(items) == {"answerable": 0.5, "verdict": 0.25, "no_evidence": 0.0, "out_of_scope": 0.25}


# --- 실제로 커밋된 평가셋(eval/evalset.jsonl)이 스펙 6.8과 파서가 읽은 법령 본문에 맞는지 ---
EVALSET_PATH = Path(__file__).resolve().parent.parent / "eval" / "evalset.jsonl"
LAW_DIR = Path(__file__).resolve().parent.parent / "data" / "manual"


def shipped() -> list[EvalItem]:
    return load_evalset(EVALSET_PATH)


# 라벨 비율이 스펙 6.8 안에 있다: 답변 불가(근거 없음+범위 밖) 10~20%, 사건 판단형 5~15%(스펙은 약 10%).
def test_shipped_evalset_label_ratios_are_within_spec():
    shares = label_shares(shipped())
    assert 0.10 <= shares["no_evidence"] + shares["out_of_scope"] <= 0.20
    assert 0.05 <= shares["verdict"] <= 0.15
    assert shares["answerable"] >= 0.60  # 나머지는 대부분 정답이 있는 질문이어야 검색 지표가 의미 있다


# 커밋된 평가셋이 규칙(정답 유무, 범위 안 법령, id 중복 없음)을 모두 지킨다.
def test_shipped_evalset_passes_validation():
    assert validate_items(shipped(), set(COVERED_LAWS)) == []


# 모든 항목에 두 가지 문장(조문 어휘형, 일상어형)이 있다 — 어휘 차이에 따른 점수 차를 재려면 둘 다 필요하다.
def test_shipped_evalset_has_both_phrasings_for_every_item():
    assert all(i.question.strip() and (i.casual_question or "").strip() for i in shipped())


# 정답 조문이 data/manual의 실제 법령 파일에서 파서가 읽은 조문과 일치한다(파일이 없는 환경에서는 건너뛴다).
@pytest.mark.skipif(not LAW_DIR.is_dir(), reason="data/manual(법령 파일)이 이 PC에 없다")
def test_shipped_gold_articles_exist_in_the_parsed_law_files():
    from scripts.build_index import load_law_chunks

    known = {(c.law_name, c.article_no) for c in load_law_chunks(LAW_DIR)}
    assert check_gold_exists(shipped(), known) == []


# --- 어려운 거절 질문(eval/evalset_hard.jsonl): 근로 분야인데 수집한 법령에 답이 없는 질문 (Task 17 후속) ---
# 메인 평가셋(40건)에는 더하지 않는다: 거절 질문 비율을 10~20%로 묶은 위 규칙(스펙 6.8)이 깨지기 때문이다.
HARD_PATH = Path(__file__).resolve().parent.parent / "eval" / "evalset_hard.jsonl"


def hard() -> list[EvalItem]:
    return load_evalset(HARD_PATH)


# 커밋된 파일이 eval/drafts.py의 원본과 같다(손으로 고친 파일이 원본과 어긋나는 일을 막는다).
def test_shipped_hard_evalset_matches_its_source():
    from eval.drafts import build_hard_evalset

    assert hard() == build_hard_evalset()


# 모두 거절 대상 라벨(근거 없음 또는 범위 밖)이고 정답 조문이 없으며, 두 가지 문장이 다 있고, 규칙 검사를 통과한다.
def test_hard_evalset_items_are_refusal_targets_with_both_phrasings():
    items = hard()
    assert len(items) >= 10
    assert all(i.label in ("no_evidence", "out_of_scope") and not i.gold for i in items)
    assert all(i.question.strip() and (i.casual_question or "").strip() for i in items)
    assert validate_items(items, set(COVERED_LAWS)) == []


# 메인 평가셋과 id가 겹치지 않는다(결과 파일을 합쳐 볼 때 섞이지 않게).
def test_hard_item_ids_do_not_clash_with_the_main_evalset():
    assert not {i.id for i in hard()} & {i.id for i in shipped()}


# "코퍼스에 답이 없다"는 주장의 근거: 각 질문의 핵심 용어가 파서가 읽은 청크 어디에도 없다. 법령 파일이 바뀌면 이 시험이 알려 준다.
@pytest.mark.skipif(not LAW_DIR.is_dir(), reason="data/manual(법령 파일)이 이 PC에 없다")
def test_hard_item_key_terms_are_absent_from_the_corpus():
    from eval.drafts import HARD_ABSENT_TERMS
    from scripts.build_index import load_law_chunks

    chunks = load_law_chunks(LAW_DIR)
    # "근거 없음" 항목(h)에는 모두 "없다고 확인한 용어"가 있다. 장난·엉뚱 입력(x)은 근로 분야가 아니라서 용어 확인 대상이 아니다.
    assert set(HARD_ABSENT_TERMS) == {i.id for i in hard() if i.label == "no_evidence"}
    for item_id, terms in HARD_ABSENT_TERMS.items():
        assert terms, item_id
        for term in terms:
            assert not [c for c in chunks if term in c.text], f"{item_id}: '{term}'이(가) 코퍼스에 있다"


# --hard 명령은 어려운 질문 파일을 만들고, 이미 있으면 덮어쓰지 않는다.
def test_drafts_cli_writes_the_hard_set_and_refuses_to_overwrite(tmp_path, capsys):
    from eval.drafts import build_hard_evalset, main

    out = tmp_path / "hard.jsonl"
    assert main(["--hard", "--out", str(out)]) == 0
    assert load_evalset(out) == build_hard_evalset()
    assert main(["--hard", "--out", str(out)]) == 1
    assert "덮어쓰지" in capsys.readouterr().out


# 장난·엉뚱 입력(x01~)은 "범위 밖" 라벨이고, 웃음·무의미한 글자·욕설·지시 무시 시도·근로와 엉뚱한 것이 섞인 입력을 모두 포함한다.
def test_hard_evalset_includes_joke_and_prompt_injection_inputs():
    odd = [i for i in hard() if i.id.startswith("x")]
    assert len(odd) >= 6 and all(i.label == "out_of_scope" for i in odd)
    texts = " ".join(i.casual_question or "" for i in odd)
    assert "무시" in texts and "ㅋㅋ" in texts  # 지시 무시 시도와 웃음 입력이 들어 있다
