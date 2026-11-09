from pathlib import Path

import pytest

from osa_tools import vault
from osa_tools.errors import OsaError
from tests.conftest import PASSWORD


def _password_file(tmp_path: Path, password: str = PASSWORD) -> Path:
    target = tmp_path / "pw"
    target.write_text(password + "\n", encoding="utf-8")
    return target


def test_short_passwords_are_refused() -> None:
    with pytest.raises(OsaError, match="at least 20 characters"):
        vault.check_new_password("x" * 19)


def test_password_of_minimum_length_is_accepted() -> None:
    assert vault.check_new_password("x" * 20) == "x" * 20


def test_encrypt_and_decrypt_round_trip(tmp_path: Path) -> None:
    password_file = _password_file(tmp_path)
    plain = tmp_path / "plain"
    plain.write_text("secret value\n", encoding="utf-8")
    encrypted = tmp_path / "encrypted"
    restored = tmp_path / "restored"
    vault.encrypt_to(plain, password_file, encrypted)
    assert encrypted.read_text(encoding="utf-8").startswith("$ANSIBLE_VAULT;")
    vault.decrypt_to(encrypted, password_file, restored)
    assert restored.read_text(encoding="utf-8") == "secret value\n"


def test_wrong_password_is_named(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.write_text("x", encoding="utf-8")
    encrypted = tmp_path / "encrypted"
    vault.encrypt_to(plain, _password_file(tmp_path), encrypted)
    wrong = tmp_path / "wrong"
    wrong.write_text("another-password-that-is-long\n", encoding="utf-8")
    with pytest.raises(OsaError, match="wrong vault password"):
        vault.decrypt_to(encrypted, wrong, tmp_path / "out")


def test_vaulted_files_lists_only_encrypted_files(tmp_path: Path) -> None:
    password_file = _password_file(tmp_path)
    plain = tmp_path / "plain.txt"
    plain.write_text("x", encoding="utf-8")
    nested = tmp_path / "secrets"
    nested.mkdir()
    vault.encrypt_to(plain, password_file, nested / "b.env")
    vault.encrypt_to(plain, password_file, tmp_path / "a.vault")
    assert vault.vaulted_files(tmp_path) == [tmp_path / "a.vault", nested / "b.env"]


def test_rekey_changes_the_password(tmp_path: Path) -> None:
    old = _password_file(tmp_path)
    new = tmp_path / "new"
    new.write_text("a-completely-new-password-9\n", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.write_text("payload", encoding="utf-8")
    encrypted = tmp_path / "enc"
    vault.encrypt_to(plain, old, encrypted)
    assert vault.rekey([encrypted], old, new) == 0
    out = tmp_path / "out"
    vault.decrypt_to(encrypted, new, out)
    assert out.read_text(encoding="utf-8") == "payload"
    with pytest.raises(OsaError):
        vault.decrypt_to(encrypted, old, tmp_path / "out2")


def test_rekey_without_files_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="No vault-encrypted files"):
        vault.rekey([], tmp_path / "a", tmp_path / "b")


def test_manual_action_runs_in_the_given_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path | None]] = []

    def fake_run(argv: list[str], cwd: Path | None = None) -> int:
        calls.append((argv, cwd))
        return 7

    monkeypatch.setattr(vault, "run", fake_run)
    result = vault.run_manual("view", ["secrets/x"], Path("/pw"), tmp_path)
    assert result == 7
    expected = ["ansible-vault", "view", "--vault-password-file", "/pw", "secrets/x"]
    assert calls == [(expected, tmp_path)]


def test_unknown_manual_action_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="Unknown vault action"):
        vault.run_manual("rekey", ["x"], Path("/pw"), tmp_path)


def test_manual_action_needs_files(tmp_path: Path) -> None:
    with pytest.raises(OsaError, match="needs at least one file"):
        vault.run_manual("edit", [], Path("/pw"), tmp_path)
