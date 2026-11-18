"""법제처에서 법령 본문과 고용노동부 법령해석을 내려받아 JSON 파일로 저장한다.

사용법(프로젝트 루트에서, 법제처가 등록한 서버 IP에서 실행 — 다른 IP에서는 인증이 거부된다):
    python scripts/collect.py laws --out data/raw
    python scripts/collect.py interpretations --out data/raw
인증값은 `.env`(또는 환경 변수)의 `LABOR_LAW_API_OC`에서 읽는다.

저장 규칙: 항목마다 `<out>/<종류>/<id>.json` 한 파일. **이미 있는 파일은 다시 받지 않는다**(여러 번 실행해도
같은 결과 — 멱등). 중간에 끊겨도 이어서 받을 수 있고, 법제처 서버 호출도 아낀다.
- laws: 파일 이름 = 법령일련번호(MST). 법령이 개정되면 새 MST가 생겨 새 파일이 생기고 옛 파일은 지우지 않는다.
  옛 버전 정리는 색인 단계(Task 7)에서 시행일자 기준으로 한다.
- interpretations: 파일 이름 = 법령해석일련번호.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# `python scripts/collect.py`로 직접 실행하면 sys.path[0]이 scripts/ 폴더라서 `import app`이 안 된다.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters.law_api import (  # noqa: E402
    KEY_INTERP_ID,
    KEY_LAW_EF_YD,
    KEY_LAW_MST,
    KEY_LAW_NAME,
    LawApiClient,
)
from app.domain.errors import LawApiError  # noqa: E402
from app.scope import COVERED_LAWS  # noqa: E402
from scripts.envfile import read_env  # noqa: E402

OC_ENV = "LABOR_LAW_API_OC"


class LawApi(Protocol):
    """수집 함수가 필요로 하는 클라이언트의 모양. 테스트에서는 가짜 객체를 넣는다."""

    def search_laws(self, name: str, page: int = 1, display: int = 100, *, current_only: bool = True) -> list[dict]: ...
    def get_law(self, mst: str, ef_yd: str | None = None) -> dict[str, Any]: ...
    def search_interpretations(self, page: int = 1, display: int = 100) -> tuple[list[dict], int]: ...
    def get_interpretation(self, interp_id: str) -> dict[str, Any]: ...


@dataclass
class CollectReport:
    """수집 결과 요약. saved: 새로 받은 수, skipped: 이미 있어서 건너뛴 수, missing: 검색에서 못 찾은 법령명."""

    saved: int = 0
    skipped: int = 0
    missing: list[str] = field(default_factory=list)


def _field(item: dict, key: str) -> str:
    """목록 항목에서 필드를 읽는다. 없으면 가이드와 실제 응답이 다르다는 뜻이므로 크게 알린다."""
    value = item.get(key)
    if value is None or str(value).strip() == "":
        raise LawApiError(f"목록 항목에 '{key}' 필드가 없습니다(가이드와 실제 응답이 다른지 확인): 키={sorted(item)}")
    return str(value)


def _save_if_absent(path: Path, fetch, report: CollectReport) -> None:
    """파일이 없을 때만 `fetch()`로 받아 저장한다. 임시 파일에 쓴 뒤 바꿔치기해서 반쯤 쓰인 파일이 남지 않게 한다."""
    if path.exists():
        report.skipped += 1
        return
    data = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # 한글이 깨지지 않게 utf-8로 쓴다(Windows 기본 인코딩은 cp949).
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    report.saved += 1


def collect_laws(client: LawApi, out_dir: Path, *, names: Sequence[str] | None = None) -> CollectReport:
    """`names`(기본: 수집 대상 법령 목록)의 현행 법령 본문을 `<out>/laws/<MST>.json`으로 저장한다.

    검색은 부분 일치라 "근로기준법"을 찾으면 시행령·시행규칙도 따라온다. 그것들은 목록에 따로 있으니
    여기서는 이름이 **정확히 같은** 법령만 받는다. 정확히 같은 이름이 없으면 `missing`에 적어 알려 준다
    (그 이름을 API가 쓰는 정확한 법령명으로 `app/scope.py`에서 고쳐야 한다).
    """
    report = CollectReport()
    for name in COVERED_LAWS if names is None else names:
        exact = [row for row in client.search_laws(name) if row.get(KEY_LAW_NAME) == name]
        if not exact:
            report.missing.append(name)
            continue
        for row in exact:
            mst = _field(row, KEY_LAW_MST)
            ef_yd = _field(row, KEY_LAW_EF_YD)
            _save_if_absent(out_dir / "laws" / f"{mst}.json", lambda m=mst, e=ef_yd: client.get_law(m, e), report)
    return report


def collect_interpretations(client: LawApi, out_dir: Path, *, page_size: int = 100) -> CollectReport:
    """고용노동부 법령해석 전체를 `<out>/interpretations/<일련번호>.json`으로 저장한다.

    목록을 쪽(`page_size`건씩)으로 넘겨 가며 전체 건수(total)만큼 모은다. 빈 쪽이 나오면 거기서 멈춘다
    (서버가 알려 준 전체 건수와 실제 목록이 어긋나도 무한 반복하지 않게 하는 안전장치).
    """
    report = CollectReport()
    page, seen = 1, 0
    while True:
        items, total = client.search_interpretations(page=page, display=page_size)
        if not items:
            break
        for item in items:
            interp_id = _field(item, KEY_INTERP_ID)
            _save_if_absent(
                out_dir / "interpretations" / f"{interp_id}.json",
                lambda i=interp_id: client.get_interpretation(i),
                report,
            )
        seen += len(items)
        if seen >= total:
            break
        page += 1
    return report


def run_collect(
    kind: str, client: LawApi, out_dir: Path, *, names: Sequence[str] | None = None
) -> CollectReport:
    """종류(`laws` | `interpretations`)에 맞는 수집 함수를 부른다."""
    if kind == "laws":
        return collect_laws(client, out_dir, names=names)
    if kind == "interpretations":
        return collect_interpretations(client, out_dir)
    raise ValueError(f"알 수 없는 종류: {kind}")


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None, client: LawApi | None = None) -> int:
    """명령줄 진입점. 종료 코드: 0 = 끝, 1 = 법제처 API 오류, 2 = 인증값 없음, 3 = 못 찾은 법령명이 있음."""
    parser = argparse.ArgumentParser(description="법제처 법령·고용노동부 법령해석을 JSON 파일로 수집한다.")
    parser.add_argument("kind", choices=["laws", "interpretations"])
    parser.add_argument("--out", default="data/raw", help="저장할 폴더(기본: data/raw)")
    args = parser.parse_args(argv)

    if client is None:
        oc = (read_env() if env is None else env).get(OC_ENV)
        if not oc:
            print(f"{OC_ENV}가 비어 있습니다. `.env`에 법제처 인증값을 넣고 다시 실행하세요.", file=sys.stderr)
            return 2
        client = LawApiClient(oc)

    try:
        report = run_collect(args.kind, client, Path(args.out))
    except LawApiError as exc:
        print(f"법제처 API 오류: {exc}", file=sys.stderr)
        return 1

    print(f"kind={args.kind} saved={report.saved} skipped={report.skipped} missing={report.missing}")
    return 3 if report.missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
