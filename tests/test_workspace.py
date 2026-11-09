import stat
from pathlib import Path

import pytest

from osa_tools.errors import OsaError
from osa_tools.workspace import Workspace


def test_directory_is_private_and_removed_on_exit(tmp_path: Path) -> None:
    with Workspace(tmp_path) as workspace:
        target = workspace.write("secret", "value")
        directory = target.parent
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_text(encoding="utf-8") == "value"
    assert not directory.exists()


def test_directory_is_removed_when_the_body_fails(tmp_path: Path) -> None:
    with Workspace(tmp_path) as workspace:
        directory = workspace.path("x").parent
        assert directory.is_dir()
        with pytest.raises(RuntimeError):
            _fail()
    assert not directory.exists()


def test_directory_is_removed_when_an_exception_leaves_the_block(
    tmp_path: Path,
) -> None:
    holder: list[Path] = []
    with pytest.raises(RuntimeError):
        _open_and_fail(tmp_path, holder)
    assert not holder[0].exists()


def test_missing_base_directory_is_refused(tmp_path: Path) -> None:
    with (
        pytest.raises(OsaError, match="memory-backed storage"),
        Workspace(tmp_path / "missing"),
    ):
        pass


def test_path_requires_an_open_workspace(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="not open"):
        Workspace(tmp_path).path("x")


def test_path_is_inside_the_workspace(tmp_path: Path) -> None:
    with Workspace(tmp_path) as workspace:
        assert workspace.path("a").name == "a"
        assert workspace.path("a").parent.parent == tmp_path


def _fail() -> None:
    raise RuntimeError


def _open_and_fail(base: Path, holder: list[Path]) -> None:
    with Workspace(base) as workspace:
        holder.append(workspace.path("x").parent)
        _fail()
