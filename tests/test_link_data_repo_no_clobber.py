from __future__ import annotations

import hashlib
import shutil
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4096), b""):
            h.update(chunk)
    return h.hexdigest()


def _preflight(source_dir: Path, target_dir: Path) -> tuple[list[tuple[Path, Path]], list[str]]:
    missing: list[tuple[Path, Path]] = []
    conflicts: list[str] = []
    for source in sorted(source_dir.rglob("*")):
        if not source.is_file() or ".git" in source.parts:
            continue
        relative = source.relative_to(source_dir)
        target = target_dir / relative
        if not target.exists():
            missing.append((source, target))
        elif not target.is_file() or _sha256(source) != _sha256(target):
            conflicts.append(str(relative))
    return missing, conflicts


def test_data_repo_preflight_conflict_aborts_without_copy_or_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "apex-trader" / "data"
    target = tmp_path / "apex-trader-data"
    source.mkdir(parents=True)
    target.mkdir()
    (source / "apex_shadow.db").write_bytes(b"source database state")
    (target / "apex_shadow.db").write_bytes(b"different target state")
    (source / "new.json").write_text('{"new": true}', encoding="utf-8")
    original_target = (target / "apex_shadow.db").read_bytes()

    missing, conflicts = _preflight(source, target)

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

    missing, conflicts = _preflight(source, target)

    assert conflicts == []
    assert [str(src.relative_to(source)) for src, _ in missing] == ["nested/new.json"]
    for src, dst in missing:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    assert (target / "nested" / "new.json").read_text(encoding="utf-8") == '{"new": true}'
    assert (target / "same.json").read_text(encoding="utf-8") == '{"same": true}'
