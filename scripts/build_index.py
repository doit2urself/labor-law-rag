"""법령 파일을 읽어 임베딩하고 OpenSearch 색인에 넣는다 (Task 7).

사용법(프로젝트 루트에서):
    python scripts/build_index.py --dry-run          # 읽고 청크 수만 센다(키·서버 불필요, 네트워크 안 씀)
    python scripts/build_index.py                    # 임베딩 + 색인

입력: `data/manual/*.doc`(국가법령정보센터에서 저장한 RTF 18개, 스펙 개정 1). 파싱은 app/ingest/parse_law.py.
설정값은 `.env`/환경 변수에서 읽는다(이름은 .env.example 참고):
    LABOR_GEMINI_API_KEY (필수), LABOR_OPENSEARCH_PASSWORD (필수),
    LABOR_OPENSEARCH_URL, LABOR_OPENSEARCH_USER, LABOR_INDEX_NAME, LABOR_EMBEDDING_MODEL, LABOR_EMBEDDING_DIM
    (뒤의 다섯 개는 없으면 app/config.py의 기본값을 쓴다).
서버의 OpenSearch는 127.0.0.1에만 열려 있으므로, 이 PC에서 돌릴 때는 SSH 터널을 열고
`LABOR_OPENSEARCH_URL=https://127.0.0.1:19200`을 지정한다(deploy/opensearch-notes.md 6절).

종료 코드: 0 성공, 1 설정·입력 문제, 2 무료 한도 초과로 중단(다시 실행하면 이어서 채운다).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

# `python scripts/build_index.py`로 직접 실행하면 sys.path[0]이 scripts/ 폴더라서 `import app`이 안 된다.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.gemini import GeminiEmbedder  # noqa: E402
from app.adapters.opensearch_store import OpenSearchStore, make_client  # noqa: E402
from app.config import Settings  # noqa: E402
from app.domain.errors import QuotaExceeded  # noqa: E402
from app.domain.models import Chunk  # noqa: E402
from app.ingest.indexer import build_index  # noqa: E402
from app.ingest.parse_law import parse_law_file  # noqa: E402
from app.ports import EmbedderPort, IndexPort  # noqa: E402
from scripts.envfile import read_env  # noqa: E402

DEFAULT_LAWS_DIR = ROOT / "data" / "manual"


def load_law_chunks(directory: str | Path) -> list[Chunk]:
    """폴더의 `.doc` 법령 파일을 모두 읽어 청크 목록으로 만든다. 파일이 하나도 없으면 오류(폴더 경로 실수를 일찍 잡는다)."""
    files = sorted(Path(directory).glob("*.doc"))
    if not files:
        raise FileNotFoundError(f"{directory} 안에 .doc 법령 파일이 없습니다")
    chunks: list[Chunk] = []
    for path in files:
        chunks.extend(parse_law_file(path))
    return chunks


def _default(name: str):
    """Settings의 기본값을 읽는다(스크립트와 앱의 기본값이 어긋나지 않게 한 곳에서 가져온다)."""
    return Settings.model_fields[name].default


def _summarize(chunks: list[Chunk]) -> None:
    """읽은 청크의 요약을 출력한다: 총 수, 법령별 수, 가장 긴 청크 길이."""
    counts = Counter(c.law_name for c in chunks)
    print(f"총 청크 {len(chunks)}개 (법령 {len(counts)}종, 가장 긴 청크 {max(len(c.text) for c in chunks)}자)")
    for name, n in sorted(counts.items()):
        print(f"  - {name}: {n}")


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    embedder: EmbedderPort | None = None,
    index: IndexPort | None = None,
) -> int:
    """스크립트 본체. env·embedder·index는 테스트에서 가짜를 넣으려고 바꿔 끼울 수 있다(없으면 진짜를 만든다)."""
    parser = argparse.ArgumentParser(description="법령 파일을 임베딩해 OpenSearch에 색인한다")
    parser.add_argument("--laws-dir", default=str(DEFAULT_LAWS_DIR), help="법령 .doc 파일 폴더")
    parser.add_argument("--batch-size", type=int, default=50, help="임베딩 요청 하나에 담을 청크 수")
    parser.add_argument("--dry-run", action="store_true", help="파일을 읽고 청크 수만 센다(네트워크·키 불필요)")
    args = parser.parse_args(argv)
    env = read_env() if env is None else env

    try:
        chunks = load_law_chunks(args.laws_dir)
    except FileNotFoundError as exc:
        print(f"오류: {exc}")
        return 1
    _summarize(chunks)
    if args.dry_run:
        print("dry-run: 임베딩과 색인은 하지 않았습니다.")
        return 0

    # 필요한 값이 없으면 한도를 쓰기 전에 알리고 멈춘다.
    missing = [
        name
        for name, needed in (
            ("LABOR_GEMINI_API_KEY", embedder is None),
            ("LABOR_OPENSEARCH_PASSWORD", index is None),
        )
        if needed and not env.get(name)
    ]
    if missing:
        print(f"오류: .env 또는 환경 변수에 {', '.join(missing)} 값이 없습니다.")
        return 1

    model = env.get("LABOR_EMBEDDING_MODEL", _default("embedding_model"))
    dim = int(env.get("LABOR_EMBEDDING_DIM", _default("embedding_dim")))
    index_name = env.get("LABOR_INDEX_NAME", _default("index_name"))
    if embedder is None:
        embedder = GeminiEmbedder(env["LABOR_GEMINI_API_KEY"], model=model, dim=dim, batch_size=args.batch_size)
    if index is None:
        url = env.get("LABOR_OPENSEARCH_URL", _default("opensearch_url"))
        client = make_client(url, env.get("LABOR_OPENSEARCH_USER", _default("opensearch_user")),
                             env["LABOR_OPENSEARCH_PASSWORD"])
        index = OpenSearchStore(client, index_name)
        print(f"색인 대상: {url} 의 '{index_name}' (모델 {model}, {dim}차원)")

    started = time.monotonic()
    try:
        report = build_index(chunks, embedder, index, batch_size=args.batch_size, dim=dim, model=model)
    except QuotaExceeded as exc:
        wait = f" 서버가 알려 준 대기 시간 약 {exc.retry_after_s:.0f}초." if exc.retry_after_s else ""
        print(f"무료 한도 초과로 멈췄습니다.{wait} 앞 배치는 이미 색인에 들어갔습니다. "
              "시간을 두고 다시 실행하면 처음부터 다시 임베딩해 채웁니다(같은 청크는 덮어쓰기라 중복되지 않습니다).")
        return 2
    elapsed = time.monotonic() - started

    quota_errors = getattr(embedder, "quota_errors", None)
    print(f"완료: 색인 {report.indexed} · 제외 {report.skipped_holdout} · 삭제 {report.deleted} · 소요 {elapsed:.1f}초"
          + (f" · 한도 초과(429) 발생 {quota_errors}회" if quota_errors is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
