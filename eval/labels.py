"""사람 검수 시트: 평가셋을 CSV로 내보내고, 사람이 확인한 결과만 다시 가져온다.

역할: 일상어 질문(`casual_question`)은 Claude가 초안을 쓰지만, "정말 사람들이 이렇게 묻는가", "정답 조문이 맞는가"는
사람이 확인해야 한다(스펙 6.8, 사용자 결정 2026-11-18). 이 모듈이 그 확인을 CSV 한 장으로 주고받게 한다.
사용처: Task 9 Step 6(검수), Task 17(검증기 라벨 시트도 같은 원칙을 쓴다).

시트 열: id, 원문질문, 일상어질문, 정답조문, 검수자, 승인
- 사용자가 채우는 열: `일상어질문`(고치고 싶으면 고친다), `검수자`(본인 이름), `승인`(O 또는 X).
- 읽기 전용 참고 열: `원문질문`(조문 어휘형 질문), `정답조문`(이 질문의 정답이 되는 조문 — 일상어 질문이 이 조문을 묻는지 판단하는 기준).

사용법(프로젝트 루트에서):
    python -m eval.labels export --out data/evalset_review.csv      # 시트 만들기(이미 있으면 덮어쓰지 않는다)
    (사용자가 시트를 열어 검수자·승인 칸을 채우고 저장한다)
    python -m eval.labels import --sheet data/evalset_review.csv    # 검수 결과를 eval/evalset.jsonl에 반영

규칙 — 사람이 아닌 검수자의 승인은 가져오지 않는다: 질문을 만든 모델(llm, claude, gemini)이 스스로 승인하면
"사람이 확인한 건수"가 부풀려지고, 평가가 자기 점수를 스스로 올리게 된다(스펙 6.8 "라벨은 생성에 쓴 모델이 대신 달지 않습니다").
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import io
import random
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from app.ingest.parse_law import article_label
from eval.evalset import EvalItem, article_sort_key, load_evalset, save_evalset
from eval.metrics import ItemResult

COLUMNS = ["id", "원문질문", "일상어질문", "정답조문", "검수자", "승인"]
REQUIRED_ON_IMPORT = ["id", "일상어질문", "검수자", "승인"]  # 가져올 때 꼭 있어야 하는 열(나머지는 읽기 전용 참고용)
NON_HUMAN_REVIEWERS = {"llm", "claude", "gemini"}


def _gold_text(item: EvalItem) -> str:
    """정답 조문을 사람이 읽는 글로 만든다. 예: "근로기준법 제55조; 근로기준법 시행령 제30조"."""
    if not item.gold:
        return "(정답 조문 없음 — 거절해야 하는 질문)"
    return "; ".join(f"{law} {article_label(article)}" for law, article in sorted(item.gold, key=article_sort_key))


def export_casual_sheet(items: list[EvalItem], path: str | Path) -> None:
    """검수 시트를 CSV로 쓴다. 검수자·승인 칸은 비워 둔다.

    `utf-8-sig`(BOM 포함)로 쓰는 이유: 한국어 Windows의 Excel은 BOM이 없는 UTF-8 CSV의 한글을 깨뜨려 보여 준다.
    """
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(COLUMNS)
        for item in items:
            writer.writerow([item.id, item.question, item.casual_question or "", _gold_text(item), "", ""])


def _read_rows(path: str | Path, required: Sequence[str] = tuple(REQUIRED_ON_IMPORT)) -> list[dict[str, str]]:
    """CSV를 읽어 줄마다 {열 이름: 값}으로 돌려준다. required: 꼭 있어야 하는 열(시트 종류마다 다르다).

    한국어 Windows의 Excel은 "CSV"로 저장하면 **cp949(BOM 없음)** 로 쓴다. UTF-8로 먼저 읽어 보고 안 되면 cp949로 읽는다.
    """
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    # StringIO(newline="")로 읽는 이유: 칸 안의 줄바꿈(검증기 시트의 인용 문서 본문)이 그대로 보존된다.
    # text.splitlines()로 먼저 자르면 줄바꿈이 사라져 칸 안의 글이 이어 붙는다("첫째\n\n둘째" → "첫째둘째", 이 PC에서 확인).
    reader = csv.DictReader(io.StringIO(text, newline=""))
    missing = [c for c in required if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"시트에 필수 열이 없습니다: {', '.join(missing)}")
    return [{k: (v or "").strip() for k, v in row.items() if k is not None} for row in reader]


def import_casual_sheet(path: str | Path, items: list[EvalItem]) -> list[EvalItem]:
    """검수된 시트를 평가셋에 반영한 새 목록을 돌려준다(원래 목록은 바꾸지 않는다).

    줄마다:
    - 검수자·승인이 둘 다 비어 있음 → 아직 안 본 줄. 건너뛴다(일부만 검수해도 가져올 수 있다).
    - 승인 O → `일상어질문` 칸의 글(고쳤다면 고친 글)을 쓰고 `reviewer`에 검수자 이름을 남긴다.
    - 승인 X → 그 일상어 질문은 쓰지 않는다(None). 사람이 봤다는 기록으로 `reviewer`는 남긴다.
    앞뒤가 안 맞는 줄(승인만 있음, 검수자만 있음, O/X가 아님, 사람이 아닌 검수자, 모르는 id, 중복 id)은 ValueError다.
    하나라도 오류면 아무것도 반영하지 않는다(반쯤 가져온 상태가 생기지 않게 모든 줄을 먼저 검사한다).
    """
    rows = _read_rows(path)
    duplicates = [i for i, n in Counter(r["id"] for r in rows).items() if n > 1]
    if duplicates:
        raise ValueError(f"시트에 id가 중복되었습니다: {', '.join(duplicates)}")

    by_id = {item.id: item for item in items}
    updates: dict[str, EvalItem] = {}
    for row in rows:
        item_id, reviewer, approval = row["id"], row["검수자"], row["승인"].upper()
        if item_id not in by_id:
            raise ValueError(f"평가셋에 없는 id입니다: {item_id}")
        if not reviewer and not approval:
            continue
        if reviewer.lower() in NON_HUMAN_REVIEWERS:
            raise ValueError(f"{item_id}: 검수자 {reviewer!r}는 사람이 아닙니다. 사람이 직접 확인한 줄만 가져옵니다")
        if not reviewer:
            raise ValueError(f"{item_id}: 승인은 있는데 검수자 칸이 비어 있습니다(본인 이름을 적어 주세요)")
        if not approval:
            raise ValueError(f"{item_id}: 검수자는 있는데 승인 칸이 비어 있습니다(O 또는 X)")
        if approval not in ("O", "X"):
            raise ValueError(f"{item_id}: 승인 칸은 O 또는 X여야 합니다(받은 값: {row['승인']!r})")
        if approval == "O":
            if not row["일상어질문"]:
                raise ValueError(f"{item_id}: 승인(O)인데 일상어질문이 비어 있습니다")
            updates[item_id] = dataclasses.replace(by_id[item_id], casual_question=row["일상어질문"], reviewer=reviewer)
        else:
            updates[item_id] = dataclasses.replace(by_id[item_id], casual_question=None, reviewer=reviewer)

    return [updates.get(item.id, item) for item in items]


# --- 검증기 라벨 시트 (Task 17) ---------------------------------------------------
#
# 서비스의 LLM 검증기는 "이 문장이 인용한 문서로 뒷받침되는가"를 문장마다 판정한다. 그 판정이 믿을 만한지는 사람이 같은
# 문장을 읽고 매긴 판정과 비교해야 알 수 있다(스펙 6.8, LLM 판정기도 틀린다). 이 시트가 그 비교용 표본을 사람에게 건넨다.
# 사람이 아닌 검수자는 일상어 시트와 같은 이유로 거부한다(질문·답변을 만든 모델이 자기 검증기를 채점하면 정확도가 부풀려진다).

VALIDATOR_COLUMNS = ["id", "질문", "답변문장", "인용문서", "일치(O/X)", "검수자"]
REQUIRED_ON_VALIDATOR_IMPORT = ["id", "일치(O/X)", "검수자"]


def _validator_rows(results: Sequence[ItemResult]) -> list[tuple[dict[str, str], bool]]:
    """시트에 올릴 수 있는 문장들을 (시트 줄, 검증기 판정) 목록으로 만든다. 줄 id는 `<평가 항목 id>#<문장 번호(1부터)>`.

    시트에 올리는 문장: 나간 답변의 문장 중 **인용이 있고** 검증기 판정이 기록된 것. 인용이 없는 문장은 "인용 문서로 뒷받침되는가"를
    물을 수 없고, 판정이 없는 문장은 검증기를 채점할 기준이 없다. 거절한 질문에는 문장이 없다.
    """
    rows: list[tuple[dict[str, str], bool]] = []
    for result in results:
        if result.refused:
            continue
        for number, sentence in enumerate(result.sentences, start=1):
            if not sentence.citation_ids or sentence.verifier_ok is None:
                continue
            documents = "\n\n".join(result.sources[i] for i in sentence.citation_ids if i in result.sources)
            row = {"id": f"{result.item_id}#{number}", "질문": result.question, "답변문장": sentence.text, "인용문서": documents}
            rows.append((row, sentence.verifier_ok))
    return rows


def export_validator_sheet(results: Sequence[ItemResult], path: str | Path, n: int = 50, seed: int = 42) -> None:
    """검증기 라벨 시트를 CSV로 쓴다: 문장이 n개보다 많으면 시드로 n개를 뽑는다(같은 시드면 항상 같은 문장, 줄 순서는 결과 순서).

    검증기의 판정은 시트에 넣지 않는다 — 사람이 그 판정을 보고 따라 표시하면(기준점 효과) 정확도가 부풀려진다.
    이미 파일이 있으면 FileExistsError: 사람이 채운 시트를 덮어쓰면 안 된다. 일치(O/X)·검수자 칸은 비워 둔다.
    """
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path} 이(가) 이미 있습니다. 사람이 고친 시트일 수 있어서 덮어쓰지 않습니다")
    rows = [row for row, _ in _validator_rows(results)]
    if len(rows) > n:
        keep = set(random.Random(seed).sample(range(len(rows)), n))
        rows = [row for index, row in enumerate(rows) if index in keep]
    with open(path, "w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=VALIDATOR_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "일치(O/X)": "", "검수자": ""})


def read_validator_sheet(path: str | Path) -> dict[str, bool]:
    """사람이 채운 시트를 {줄 id: 사람 판정(True=일치)}로 읽는다. 아직 안 채운 줄은 건너뛴다(일부만 채워도 가져올 수 있다).

    앞뒤가 안 맞는 줄(검수자만 있음, 일치 칸만 있음, O/X가 아님, 사람이 아닌 검수자, 중복 id)은 ValueError다.
    """
    rows = _read_rows(path, REQUIRED_ON_VALIDATOR_IMPORT)
    duplicates = [i for i, count in Counter(r["id"] for r in rows).items() if count > 1]
    if duplicates:
        raise ValueError(f"시트에 id가 중복되었습니다: {', '.join(duplicates)}")
    labels: dict[str, bool] = {}
    for row in rows:
        row_id, mark, reviewer = row["id"], row["일치(O/X)"].upper(), row["검수자"]
        if not mark and not reviewer:
            continue
        if reviewer.lower() in NON_HUMAN_REVIEWERS:
            raise ValueError(f"{row_id}: 검수자 {reviewer!r}는 사람이 아닙니다. 사람이 직접 확인한 줄만 가져옵니다")
        if not reviewer:
            raise ValueError(f"{row_id}: 일치 표시는 있는데 검수자 칸이 비어 있습니다(본인 이름을 적어 주세요)")
        if not mark:
            raise ValueError(f"{row_id}: 검수자는 있는데 일치(O/X) 칸이 비어 있습니다")
        if mark not in ("O", "X"):
            raise ValueError(f"{row_id}: 일치(O/X) 칸은 O 또는 X여야 합니다(받은 값: {row['일치(O/X)']!r})")
        labels[row_id] = mark == "O"
    return labels


def import_validator_sheet(path: str | Path) -> list[bool]:
    """사람이 채운 줄들의 판정(True=일치)을 시트 순서대로 돌려준다. 어느 문장의 판정인지는 `read_validator_sheet`가 id와 함께 준다."""
    return list(read_validator_sheet(path).values())


def predicted_flags(results: Sequence[ItemResult], keys: Sequence[str]) -> list[bool]:
    """검증기가 같은 문장에 낸 판정을 `keys`(사람이 채운 줄 id) 순서대로 꺼낸다. `validator_accuracy`의 predicted 입력이 된다.

    모르는 id면 KeyError — 시트가 이 결과 파일과 다른 실행에서 나온 것이라는 뜻이라 조용히 넘기지 않는다.
    """
    by_key = {row["id"]: verdict for row, verdict in _validator_rows(results)}
    return [by_key[key] for key in keys]


DEFAULT_EVALSET = Path(__file__).resolve().parent / "evalset.jsonl"


def main(argv: Sequence[str] | None = None) -> int:
    """`export`(시트 만들기)와 `import`(검수 결과 반영) 두 명령. 종료 코드: 0 성공, 1 오류(파일은 건드리지 않는다)."""
    parser = argparse.ArgumentParser(description="평가셋 사람 검수 시트를 내보내고 가져온다")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="검수 시트(CSV)를 만든다")
    export.add_argument("--evalset", default=str(DEFAULT_EVALSET))
    export.add_argument("--out", required=True, help="만들 CSV 경로(이미 있으면 덮어쓰지 않는다)")
    load = sub.add_parser("import", help="검수된 시트를 평가셋에 반영한다")
    load.add_argument("--evalset", default=str(DEFAULT_EVALSET))
    load.add_argument("--sheet", required=True, help="검수자·승인 칸을 채운 CSV")
    args = parser.parse_args(argv)

    try:
        items = load_evalset(args.evalset)
        if args.command == "export":
            out = Path(args.out)
            if out.exists():
                print(f"{out} 이(가) 이미 있습니다. 사람이 고친 시트일 수 있어서 덮어쓰지 않습니다(다른 이름을 쓰거나 먼저 옮겨 주세요).")
                return 1
            out.parent.mkdir(parents=True, exist_ok=True)
            export_casual_sheet(items, out)
            print(f"{len(items)}건을 {out} 에 내보냈습니다. 일상어질문을 읽어 보고 검수자(본인 이름)와 승인(O 또는 X)을 채워 저장하세요.")
            return 0

        updated = import_casual_sheet(args.sheet, items)
    except (OSError, ValueError) as exc:
        print(f"오류: {exc}")
        return 1

    changed = [new for old, new in zip(items, updated) if old != new]
    approved = sum(1 for i in changed if i.casual_question is not None)
    rejected = len(changed) - approved
    save_evalset(updated, args.evalset)
    reviewed_total = sum(1 for i in updated if i.reviewer)
    print(f"반영했습니다: 승인 {approved} · 반려 {rejected} · 아직 안 본 항목 {len(updated) - reviewed_total} "
          f"(사람이 확인한 항목 누계 {reviewed_total}/{len(updated)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
