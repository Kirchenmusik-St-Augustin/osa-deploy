import sys
from pathlib import Path

import pytest

from osa_tools.errors import OsaError
from osa_tools.runner import capture, run


def test_run_returns_the_exit_code(tmp_path: Path) -> None:
    assert run([sys.executable, "-c", "raise SystemExit(3)"]) == 3
    assert run([sys.executable, "-c", "pass"], cwd=tmp_path) == 0


def test_run_reports_a_missing_binary() -> None:
    with pytest.raises(OsaError, match="'no-such-binary-osa' is not installed"):
        run(["no-such-binary-osa"])


def test_capture_returns_stdout() -> None:
    assert capture([sys.executable, "-c", "print('hi')"]).strip() == "hi"


def test_capture_reports_the_tail_of_stderr() -> None:
    script = (
        "import sys\nsys.stderr.write('\\n'.join(map(str, range(20))))\nsys.exit(2)"
    )
    with pytest.raises(OsaError) as info:
        capture([sys.executable, "-c", script])
    message = str(info.value)
    assert "exit 2" in message
    assert "19" in message
    assert "14" not in message.split("exit 2):")[1]


def test_capture_reports_a_missing_binary() -> None:
    with pytest.raises(OsaError, match="is not installed"):
        capture(["no-such-binary-osa"])
