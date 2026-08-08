"""
APEX TRADER — data-dir safety guard tests (``runtime_paths``).

The single-user default data dir is ``<repo>/data``, which is supposed to be a
symlink/junction to the separate data repo. These tests lock down that
``ensure_data_dir``/``data_dir_is_safe`` refuse to materialize a plain ``data/``
*inside* the source git checkout (the footgun behind the production incident),
while still allowing symlinks, dedicated repos, and isolated dirs outside any
repo.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

import runtime_paths


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class TestDataDirIsSafe:
    def test_standalone_dir_outside_any_repo_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "isolated" / "data"
            ok, _ = runtime_paths.data_dir_is_safe(d)
            assert ok

    def test_dedicated_git_repo_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "data"
            d.mkdir()
            _init_repo(d)
            ok, reason = runtime_paths.data_dir_is_safe(d)
            assert ok
            assert "dedicated git repo" in reason

    def test_symlink_junction_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "apex-trader-data"
            real.mkdir()
            link = Path(tmp) / "data"
            os.symlink(real, link)
            ok, reason = runtime_paths.data_dir_is_safe(link)
            assert ok
            assert "symlink" in reason

    def test_plain_dir_inside_source_repo_is_unsafe(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source)
            plain_data = source / "data"
            plain_data.mkdir()
            ok, reason = runtime_paths.data_dir_is_safe(plain_data)
            assert not ok
            assert "plain directory inside a git work tree" in reason

    def test_missing_dir_inside_source_repo_is_unsafe(self):
        # Even before creation: the would-be location is nested in the source repo.
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source)
            ok, _ = runtime_paths.data_dir_is_safe(source / "data")
            assert not ok


class TestEnsureDataDir:
    def test_creates_safe_standalone_dir(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "isolated" / "data"
            monkeypatch.setenv("APEX_DATA_DIR", str(target))
            out = runtime_paths.ensure_data_dir()
            assert out == target
            assert target.is_dir()

    def test_refuses_plain_dir_inside_source_repo(self, monkeypatch):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "apex-trader"
            source.mkdir()
            _init_repo(source)
            monkeypatch.setenv("APEX_DATA_DIR", str(source / "data"))
            with pytest.raises(runtime_paths.DataJunctionError):
                runtime_paths.ensure_data_dir()
            assert not (source / "data").exists()
