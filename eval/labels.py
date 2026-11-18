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
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from app.ingest.parse_law import article_label
from eval.evalset import EvalItem, article_sort_key, load_evalset, save_evalset

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


def _read_rows(path: str | Path) -> list[dict[str, str]]:
    """CSV를 읽어 줄마다 {열 이름: 값}으로 돌려준다.

    한국어 Windows의 Excel은 "CSV"로 저장하면 **cp949(BOM 없음)** 로 쓴다. UTF-8로 먼저 읽어 보고 안 되면 cp949로 읽는다.
    """
    raw = Path(path).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp949")
    reader = csv.DictReader(text.splitlines())
    missing = [c for c in REQUIRED_ON_IMPORT if c not in (reader.fieldnames or [])]
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
