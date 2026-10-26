from __future__ import annotations

import shutil
from pathlib import Path

from scripts.link_data_repo import preflight_data_copy, verify_copied_files


def test_data_repo_preflight_conflict_aborts_without_copy_or_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "apex-trader" / "data"
    target = tmp_path / "apex-trader-data"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "apex_shadow.db").write_bytes(b"source database state")
    (target / "apex_shadow.db").write_bytes(b"different target state")
    (source / "new.json").write_text('{"new": true}', encoding="utf-8")
    original_target = (target / "apex_shadow.db").read_bytes()

    missing, conflicts = preflight_data_copy(source, target)

    assert conflicts == ["apex_shadow.db"]
    assert missing == []
    assert (target / "apex_shadow.db").read_bytes() == original_target
    assert not (target / "new.json").exists()
    assert (source / "apex_shadow.db").read_bytes() == b"source database state"


def test_data_repo_preflight_accepts_identical_and_plans_only_missing(tmp_path: Path) -> None:
    source = tmp_path / "apex-trader" / "data"
    target = tmp_path / "apex-trader-data"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "same.json").write_text('{"same": true}', encoding="utf-8")
    (target / "same.json").write_text('{"same": true}', encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "new.json").write_text('{"new": true}', encoding="utf-8")

    missing, conflicts = preflight_data_copy(source, target)

    assert conflicts == []
    assert [str(src.relative_to(source)) for src, _ in missing] == ["nested/new.json"]
    for src, dst in missing:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    assert verify_copied_files(source, target) == (True, "")
    assert (target / "nested" / "new.json").read_text(encoding="utf-8") == '{"new": true}'
    assert (target / "same.json").read_text(encoding="utf-8") == '{"same": true}'


def test_data_repo_verification_detects_change_before_relink(tmp_path: Path) -> None:
    source = tmp_path / "data"
    target = tmp_path / "data-repo"
    source.mkdir()
    target.mkdir()
    (source / "event.db").write_bytes(b"source")
    (target / "event.db").write_bytes(b"changed")

    ok, failed = verify_copied_files(source, target)

    assert ok is False
    assert failed == "event.db"
    assert (source / "event.db").read_bytes() == b"source"



