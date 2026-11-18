"""사람 검수 시트(eval/labels.py) 시험 (Task 9).

규칙의 핵심: 사람이 아닌 검수자(llm 등)의 승인은 가져오지 않는다 — 평가가 스스로 점수를 올리는 것을 막는다(스펙 6.8).
"""

import csv

import pytest

from eval.evalset import EvalItem
from eval.labels import export_casual_sheet, import_casual_sheet

LAW = "근로기준법"


def make_items() -> list[EvalItem]:
    return [
        EvalItem("a01", "유급휴일을 주어야 하나요?", "주휴수당이 뭐예요?", frozenset({(LAW, "55")}), "요약1", "answerable"),
        EvalItem("a02", "연차 유급휴가는 며칠인가요?", "연차는 며칠 생겨요?", frozenset({(LAW, "60"), (LAW, "76의2")}), "요약2", "answerable"),
        EvalItem("o01", "전세 보증금을 돌려받으려면?", "전세 보증금 돌려받는 법", frozenset(), "거절", "out_of_scope"),
    ]


def write_sheet(path, rows, *, encoding="utf-8-sig"):
    """검수자가 고쳐서 저장한 시트를 흉내 낸다."""
    header = ["id", "원문질문", "일상어질문", "정답조문", "검수자", "승인"]
    with open(path, "w", encoding=encoding, newline="") as file:
        writer = csv.writer(file)
        writer.writerow(header)
        writer.writerows(rows)


# 내보낸 시트에는 모든 항목이 한 줄씩 있고, 정답 조문이 사람이 읽는 말로 적히며, 검수자·승인 칸은 비어 있다.
def test_export_writes_one_row_per_item_with_readable_gold(tmp_path):
    path = tmp_path / "sheet.csv"
    export_casual_sheet(make_items(), path)
    with open(path, encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [r["id"] for r in rows] == ["a01", "a02", "o01"]
    assert rows[0]["정답조문"] == "근로기준법 제55조"
    assert rows[1]["정답조문"] == "근로기준법 제60조; 근로기준법 제76조의2"
    assert "거절" in rows[2]["정답조문"]
    assert rows[0]["일상어질문"] == "주휴수당이 뭐예요?" and rows[0]["검수자"] == "" and rows[0]["승인"] == ""


# 한국어 Excel이 열 수 있게 BOM(utf-8-sig)이 붙는다. 없으면 한글이 깨져 보인다.
def test_export_starts_with_a_utf8_bom(tmp_path):
    path = tmp_path / "sheet.csv"
    export_casual_sheet(make_items(), path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


# 승인(O): 시트의 일상어 질문(고쳤으면 고친 글)이 항목에 들어가고, 검수자 이름이 남는다. 시트에 없는 항목은 그대로다.
def test_import_applies_approved_and_edited_question(tmp_path):
    path = tmp_path / "s.csv"
    write_sheet(path, [["a01", "x", "알바 주휴수당이 뭐고 어떻게 받아요?", "x", "홍길동", "O"]])
    items = import_casual_sheet(path, make_items())
    assert items[0].casual_question == "알바 주휴수당이 뭐고 어떻게 받아요?" and items[0].reviewer == "홍길동"
    assert items[1] == make_items()[1] and items[2] == make_items()[2]


# 반려(X): 그 일상어 질문은 쓰지 않는다(None). 검수자는 남긴다 — 사람이 봤다는 기록이다.
def test_import_rejection_drops_the_casual_question(tmp_path):
    path = tmp_path / "s.csv"
    write_sheet(path, [["a02", "x", "연차는 며칠 생겨요?", "x", "홍길동", "x"]])
    items = import_casual_sheet(path, make_items())
    assert items[1].casual_question is None and items[1].reviewer == "홍길동"


# 검수자도 승인도 비어 있는 줄은 "아직 안 봤다"이므로 건너뛴다(일부만 검수해도 가져올 수 있다).
def test_import_skips_unreviewed_rows(tmp_path):
    path = tmp_path / "s.csv"
    write_sheet(path, [["a01", "x", "바뀐 글", "x", "", ""]])
    assert import_casual_sheet(path, make_items()) == make_items()


# LLM이 검수자로 적힌 줄은 가져오지 않고 오류로 멈춘다(대소문자·공백 무시).
@pytest.mark.parametrize("reviewer", ["llm", "LLM", " Llm ", "claude", "Gemini"])
def test_import_rejects_non_human_reviewers(tmp_path, reviewer):
    path = tmp_path / "s.csv"
    write_sheet(path, [["a01", "x", "글", "x", reviewer, "O"]])
    with pytest.raises(ValueError, match="사람"):
        import_casual_sheet(path, make_items())


# 앞뒤가 맞지 않는 줄은 조용히 넘기지 않고 오류로 알린다.
@pytest.mark.parametrize(
    "row, message",
    [
        (["a01", "x", "글", "x", "", "O"], "검수자"),        # 승인만 있고 검수자가 없다
        (["a01", "x", "글", "x", "홍길동", ""], "승인"),      # 검수자만 있고 승인이 없다
        (["a01", "x", "글", "x", "홍길동", "맞음"], "O 또는 X"),  # 승인 값이 O/X가 아니다
        (["a01", "x", "   ", "x", "홍길동", "O"], "비어"),     # 승인(O)인데 일상어 질문이 비었다
        (["zzz", "x", "글", "x", "홍길동", "O"], "zzz"),      # 평가셋에 없는 id
    ],
)
def test_import_rejects_inconsistent_rows(tmp_path, row, message):
    path = tmp_path / "s.csv"
    write_sheet(path, [row])
    with pytest.raises(ValueError, match=message):
        import_casual_sheet(path, make_items())


# 같은 id가 두 줄이면 어느 쪽이 맞는지 알 수 없으므로 오류다.
def test_import_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "s.csv"
    write_sheet(path, [["a01", "x", "글1", "x", "홍길동", "O"], ["a01", "x", "글2", "x", "홍길동", "O"]])
    with pytest.raises(ValueError, match="중복"):
        import_casual_sheet(path, make_items())


# 한국어 Windows의 Excel은 "CSV"로 저장하면 cp949(BOM 없음)가 된다. 그것도 읽어야 한다.
def test_import_reads_a_sheet_saved_as_cp949(tmp_path):
    path = tmp_path / "s.csv"
    write_sheet(path, [["a01", "x", "알바 주휴수당이 뭐예요?", "x", "홍길동", "O"]], encoding="cp949")
    assert import_casual_sheet(path, make_items())[0].reviewer == "홍길동"


# 필수 열이 없으면(다른 파일을 가져왔거나 열을 지웠다면) 어떤 열이 없는지 알려 준다.
def test_import_reports_missing_columns(tmp_path):
    path = tmp_path / "s.csv"
    path.write_text("id,일상어질문\na01,글\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="검수자"):
        import_casual_sheet(path, make_items())


# --- 명령줄: python -m eval.labels export|import ----------------------------------


def run_cli(*args):
    from eval.labels import main

    return main(list(args))


def save_items(path):
    from eval.evalset import save_evalset

    save_evalset(make_items(), path)


# 내보내기 → (사람이 시트를 고쳐 저장) → 가져오기를 명령으로 끝까지 한다. 가져온 결과는 평가셋 파일에 반영되고 요약이 출력된다.
def test_cli_export_then_import_updates_the_evalset_file(tmp_path, capsys):
    from eval.evalset import load_evalset

    evalset, sheet = tmp_path / "set.jsonl", tmp_path / "sheet.csv"
    save_items(evalset)
    assert run_cli("export", "--evalset", str(evalset), "--out", str(sheet)) == 0
    write_sheet(sheet, [["a01", "x", "알바 주휴수당 어떻게 받아요?", "x", "홍길동", "O"],
                        ["a02", "x", "연차는 며칠 생겨요?", "x", "홍길동", "X"]])
    assert run_cli("import", "--evalset", str(evalset), "--sheet", str(sheet)) == 0
    loaded = {i.id: i for i in load_evalset(evalset)}
    assert loaded["a01"].casual_question == "알바 주휴수당 어떻게 받아요?" and loaded["a01"].reviewer == "홍길동"
    assert loaded["a02"].casual_question is None and loaded["o01"].reviewer is None
    text = capsys.readouterr().out
    assert "승인 1" in text and "반려 1" in text and "아직 안 본 항목 1" in text


# 검수 시트에 오류가 있으면 평가셋 파일을 건드리지 않고 종료 코드 1로 멈춘다.
def test_cli_import_leaves_the_evalset_untouched_on_error(tmp_path, capsys):
    evalset, sheet = tmp_path / "set.jsonl", tmp_path / "sheet.csv"
    save_items(evalset)
    before = evalset.read_bytes()
    write_sheet(sheet, [["a01", "x", "글", "x", "llm", "O"]])
    assert run_cli("import", "--evalset", str(evalset), "--sheet", str(sheet)) == 1
    assert evalset.read_bytes() == before and "사람" in capsys.readouterr().out


# 이미 있는 시트를 덮어쓰지 않는다: 사람이 고친 시트를 실수로 지우는 것을 막는다.
def test_cli_export_refuses_to_overwrite_an_existing_sheet(tmp_path):
    evalset, sheet = tmp_path / "set.jsonl", tmp_path / "sheet.csv"
    save_items(evalset)
    sheet.write_text("고친 내용", encoding="utf-8")
    assert run_cli("export", "--evalset", str(evalset), "--out", str(sheet)) == 1
    assert sheet.read_text(encoding="utf-8") == "고친 내용"
