"""scripts/build_index.py 테스트: 법령 파일 읽기와 실행 흐름(인자·환경 값·종료 코드)을 서버 없이 시험한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.domain.errors import QuotaExceeded
from scripts import build_index as script
from tests.conftest import FakeEmbedder, FakeIndex
from tests.test_indexer import chunk

MANUAL = Path(__file__).resolve().parent.parent / "data" / "manual"
HAS_REAL_FILES = bool(list(MANUAL.glob("*.doc")))

ENV = {"LABOR_GEMINI_API_KEY": "k", "LABOR_OPENSEARCH_PASSWORD": "p"}


@pytest.fixture
def fake_chunks(monkeypatch):
    """파일 없이 청크 3개를 돌려주도록 읽기 단계를 바꿔 끼운다."""
    chunks = [chunk("a"), chunk("b"), chunk("c")]
    monkeypatch.setattr(script, "load_law_chunks", lambda directory: chunks)
    return chunks


# 법령 폴더에 .doc가 없으면 빈 색인을 만들지 않고 이유를 알려 준다(폴더 경로 실수를 일찍 잡는다).
def test_load_law_chunks_rejects_a_folder_without_law_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        script.load_law_chunks(tmp_path)


# 실제 18개 파일을 읽으면 청크 수백 개가 나오고 chunk_id가 겹치지 않는다(색인에서 덮어써지는 사고 방지).
@pytest.mark.skipif(not HAS_REAL_FILES, reason="data/manual 파일이 없다(저장소에 포함되지 않음)")
def test_load_law_chunks_reads_all_real_files_with_unique_ids():
    chunks = script.load_law_chunks(MANUAL)
    assert len(chunks) > 700 and len({c.chunk_id for c in chunks}) == len(chunks)


# --dry-run은 읽고 세기만 한다. 키가 없어도 돌고(네트워크를 쓰지 않음), 청크 수를 보여 준다.
def test_dry_run_needs_no_keys_and_touches_nothing(fake_chunks, capsys):
    index = FakeIndex()
    assert script.main(["--dry-run"], env={}, embedder=FakeEmbedder(), index=index) == 0
    out = capsys.readouterr().out
    assert "청크 3개" in out and "dry-run" in out and index.count() == 0


# 키가 없으면 어떤 환경 값이 빠졌는지 알려 주고 실행하지 않는다(종료 코드 1).
def test_missing_key_is_reported_before_anything_runs(fake_chunks, capsys):
    index = FakeIndex()
    assert script.main([], env={}, embedder=None, index=index) == 1
    assert "LABOR_GEMINI_API_KEY" in capsys.readouterr().out and index.count() == 0


# 정상 실행: 청크를 모두 색인에 넣고 결과 요약(색인한 수, 지운 수)을 보여 준다.
def test_full_run_indexes_everything_and_reports(fake_chunks, capsys):
    index = FakeIndex()
    assert script.main([], env=ENV, embedder=FakeEmbedder(), index=index) == 0
    out = capsys.readouterr().out
    assert index.count() == 3 and "색인 3" in out and "삭제 0" in out


# 한도 초과로 멈추면 종료 코드 2와 "다시 실행하면 이어서 채운다"는 안내를 준다.
def test_quota_error_gives_exit_code_2_and_resume_hint(fake_chunks, capsys):
    class Failing(FakeEmbedder):
        def embed_documents(self, texts):
            raise QuotaExceeded("한도", retry_after_s=90.0)

    assert script.main([], env=ENV, embedder=Failing(), index=FakeIndex()) == 2
    out = capsys.readouterr().out
    assert "다시 실행" in out and "90" in out
