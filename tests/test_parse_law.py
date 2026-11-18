"""법령 파서(app/ingest/parse_law.py)를 검증한다.

입력은 RTF에서 뽑은 글(줄 목록)이다. 테스트 글은 실제 파일(`docs/step0-report.md` 4절)에서 확인한 모양을 본떠 짧게 만들었다.
가장 중요한 것은 **시행예정 조문을 현행과 구분하는 규칙**이다(스펙 개정 1의 파서 규칙):
- 같은 조문번호가 두 번 나오면 첫 번째가 현행, 뒤는 시행예정 버전이다.
- 블록이 하나뿐인데 조 전체를 가리키는 `[시행일: …] 제N조` 표시가 붙으면 아직 시행 전인 신설 조문이라 뺀다.
- `제60조제8항`처럼 일부만 가리키는 표시는 조문 자체가 현행이므로 뺀다고 하면 안 된다.
맨 아래 한 테스트는 저장소에 없는 실제 파일(data/manual)이 있을 때만 돌고, 18개를 모두 파싱해 본다.
"""

from datetime import date
from pathlib import Path

import pytest

from app.ingest.parse_law import article_url, parse_law_file, parse_law_text
from app.scope import COVERED_LAWS

PREFIX = "법제처 국가법령정보센터"
META = "[시행 2026. 10. 8.] [법률 제21533호, 2026. 4. 7., 일부개정]"


def law_text(*body: str, name: str = "근로기준법", meta: str = META) -> str:
    """머리글(법령명 두 번, 시행일자 줄, 담당 부서 줄) + 본문 줄로 법령 글을 만든다."""
    return "\n".join([PREFIX + name, PREFIX + name, meta, "고용노동부(근로기준정책과) 044-202-7534", *body])


SAMPLE = law_text(
    "제1장 총칙",
    "제1조(목적) 이 법은 근로조건의 기준을 정함을 목적으로 한다.",
    "제2조 (정의) ① 이 법에서 사용하는 용어의 뜻은 다음과 같다. <개정 2018. 3. 20.>",
    "1. “근로자”란 임금을 목적으로 근로를 제공하는 사람을 말한다.",
    "② 제1항제6호에 따라 산출된 금액이 통상임금보다 적으면 통상임금액을 평균임금으로 한다.",
    "[제목개정 2021. 1. 5.]",
    "제12조 삭제 <2009. 1. 1.>",
    "제13조(보고) 근로감독관의 요구가 있으면 보고하여야 한다.",
    "제13조(보고) 노동감독관의 요구가 있으면 보고하여야 한다.",
    "[시행일: 2026. 12. 8.] 제13조",
    "제43조의2(체불사업주 명단 공개) 고용노동부장관은 명단을 공개한다.",
    "제44조의4(임금 직접지급) 도급인은 임금을 직접 지급한다.",
    "[본조신설 2026. 4. 7.]",
    "[시행일: 2027. 1. 1.] 제44조의4",
    "제60조(연차) ① 사용자는 휴가를 주어야 한다.",
    "[시행일: 2027. 6. 10.] 제60조제8항",
    "부칙 <제21857호,2026. 8. 4.>(형사소송법)",
    "제1조(시행일) 이 법은 2026년 10월 2일부터 시행한다.",
)


def by_no(chunks):
    return {c.article_no: c for c in chunks}


# 머리글의 시행일자 줄에서 법령명·날짜·공포번호·개정구분을 읽고, 청크 메타데이터에 채운다.
def test_header_metadata_is_filled_into_chunks():
    c = by_no(parse_law_text(SAMPLE))["2"]
    assert (c.doc_type, c.law_name, c.title) == ("law", "근로기준법", "근로기준법 제2조(정의)")
    assert (c.effective_date, c.promulgation_date) == ("2026-10-08", "2026-04-07")
    assert c.source_id == "law:근로기준법:21533:20261008"
    assert c.source_url == "https://www.law.go.kr/법령/근로기준법/제2조"
    assert c.extra["promulgation_no"] == "21533" and c.extra["law_kind"] == "법률" and c.extra["amendment"] == "일부개정"
    assert c.extra["chapter"] == "제1장 총칙"


# `제1조(목적)`과 `제1조 (목적)`(기간제법 시행규칙처럼 공백이 있는 형식)을 모두 조문 제목으로 읽는다.
def test_article_heading_allows_space_before_parenthesis():
    chunks = parse_law_text(law_text("제1조 (목적) 이 규칙은 목적을 정한다.", "제2조(정의) 정의한다."))
    assert [c.article_no for c in chunks] == ["1", "2"]


# 가지 조문은 `43의2`로 적고, 주소는 `제43조의2`가 된다.
def test_branch_article_number_and_url():
    c = by_no(parse_law_text(SAMPLE))["43의2"]
    assert c.title.startswith("근로기준법 제43조의2(") and c.source_url.endswith("/제43조의2")


# 삭제된 조문은 청크로 만들지 않는다.
def test_deleted_article_is_skipped():
    assert "12" not in by_no(parse_law_text(SAMPLE))


# 같은 조문이 두 번 나오면 첫 번째(현행)만 쓰고, 시행예정 버전(`노동감독관`)은 버린다.
def test_first_block_is_current_and_later_block_is_future():
    chunks = [c for c in parse_law_text(SAMPLE) if c.article_no == "13"]
    assert len(chunks) == 1 and "근로감독관" in chunks[0].text and "노동감독관" not in chunks[0].text


# 블록이 하나뿐인데 조 전체를 가리키는 시행일 표시가 붙으면 아직 시행 전인 신설 조문이라 뺀다.
def test_future_only_new_article_is_excluded():
    assert "44의4" not in by_no(parse_law_text(SAMPLE))


# 일부(제8항)만 가리키는 시행일 표시는 조문 자체가 현행이므로 조문을 빼면 안 된다.
def test_partial_future_marker_keeps_the_article():
    assert "60" in by_no(parse_law_text(SAMPLE))


# 부칙(다른 법률 개정 부칙 포함) 뒤의 줄은 청크로 만들지 않는다. 부칙의 `제1조(시행일)`이 본문 제1조를 덮으면 안 된다.
def test_addenda_are_excluded():
    chunks = parse_law_text(SAMPLE)
    assert by_no(chunks)["1"].text.startswith("제1조(목적)") and not any("시행일" in c.title for c in chunks)


# 청크 본문에 개정 이력 표기(<개정…>, [제목개정…])와 머리글 잡음(전화번호, 법제처 반복 줄)이 없다.
def test_annotations_and_header_noise_are_removed():
    for c in parse_law_text(SAMPLE):
        for noise in ("<개정", "<신설", "[제목개정", "[본조신설", "044-202", PREFIX, "[시행일:"):
            assert noise not in c.text, (c.article_no, noise)


# 호(`1.`)와 항(`②`) 줄이 조문 본문에 이어 붙는다.
def test_items_and_paragraphs_stay_in_article_text():
    text = by_no(parse_law_text(SAMPLE))["2"].text
    assert text.startswith("제2조 (정의) ①") and "1. “근로자”" in text and "② 제1항제6호" in text


# 장 제목이 두 가지(현행/시행예정)로 나오면 첫 번째를 쓴다.
def test_duplicate_chapter_title_uses_the_first():
    chunks = parse_law_text(law_text("제11장 근로감독관 등", "제11장 노동감독관 등", "제101조(감독 기관) 근로감독관을 둔다."))
    assert chunks[0].extra["chapter"] == "제11장 근로감독관 등"


# 조문이 길면 항 단위로 나누고, 모든 조각이 법령명·조문번호를 유지하며 앞에 조문 제목이 붙는다.
def test_long_article_splits_by_paragraph_and_keeps_metadata():
    chunks = parse_law_text(
        law_text("제5조(긴 조문) ① " + "가" * 40, "② " + "나" * 40, "③ " + "다" * 40), max_chars=100
    )
    assert [c.paragraph for c in chunks] == ["1", "2", "3"]
    assert all(c.law_name == "근로기준법" and c.article_no == "5" for c in chunks)
    assert all(c.text.startswith("제5조(긴 조문) ") for c in chunks)
    assert "나" * 40 in chunks[1].text and "가" * 40 not in chunks[1].text


# 한도 이하의 조문은 쪼개지 않는다(paragraph는 None).
def test_short_article_is_one_chunk_without_paragraph():
    chunks = parse_law_text(law_text("제5조(조문) ① 가나다", "② 라마바"), max_chars=1000)
    assert len(chunks) == 1 and chunks[0].paragraph is None


# 같은 입력이면 chunk_id가 항상 같고(다시 색인해도 덮어쓰기), 서로 겹치지 않는다.
def test_chunk_ids_are_stable_and_unique():
    ids = [c.chunk_id for c in parse_law_text(SAMPLE)]
    assert ids == [c.chunk_id for c in parse_law_text(SAMPLE)] and len(set(ids)) == len(ids)
    assert ids[0] == "law:근로기준법:21533:20261008:1:0"


# 시행일자 줄이 없으면 어떤 법령인지 알 수 없으므로 조용히 넘기지 않고 오류로 알린다.
def test_missing_header_raises_value_error():
    with pytest.raises(ValueError):
        parse_law_text("제1조(목적) 본문만 있다.")


def test_article_url():
    assert article_url("근로기준법", "55") == "https://www.law.go.kr/법령/근로기준법/제55조"
    assert article_url("근로기준법", "55의2") == "https://www.law.go.kr/법령/근로기준법/제55조의2"


# ---- 실제 파일(data/manual): 저장소에 없으므로 있을 때만 돈다 ----

MANUAL = Path(__file__).resolve().parent.parent / "data" / "manual"


@pytest.mark.skipif(not list(MANUAL.glob("*.doc")), reason="data/manual 파일이 없다(저장소에 포함되지 않음)")
def test_all_real_files_parse_and_respect_future_rules():
    files = sorted(MANUAL.glob("*.doc"))
    ids: set[str] = set()
    for path in files:
        chunks = parse_law_file(path)
        assert chunks, path.name
        for c in chunks:
            assert "[시행일:" not in c.text and PREFIX not in c.text, (path.name, c.article_no)
            assert c.effective_date <= date.today().isoformat(), path.name  # 현행 버전의 시행일자다
            assert c.chunk_id not in ids, c.chunk_id
            ids.add(c.chunk_id)
    # 파서가 읽은 법령명 18개가 수집 범위 목록과 정확히 같아야 한다(비슷해 보이는 다른 문자까지 잡는다).
    names = {parse_law_file(path)[0].law_name for path in files}
    assert names == set(COVERED_LAWS)
    labor = {c.article_no: c for c in parse_law_file(MANUAL / "근로기준법.doc")}
    assert "55" in labor and "43의6" in labor  # 현행 조문
    assert "44의4" not in labor  # 시행 전 신설 조문(시행일 2027. 1. 1.)
    assert "노동감독관" not in labor["13"].text  # 제13조는 현행(근로감독관) 문구
