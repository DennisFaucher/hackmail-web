"""Optional accounts file, mounted as a Docker secret (never baked into the image or passed as environment variables).

Format: KEY=value lines, numbered per account:

    ACCOUNT_1_EMAIL=you@gmail.com
    ACCOUNT_1_NAME=Your Full Name
    ACCOUNT_1_PASSWORD=abcd efgh ijkl mnop

Passwords are held in memory only and are never logged or returned by the API.
"""
import logging
import os
import re
import stat
from pathlib import Path

from . import imapx
from .compose import is_valid_email

log = logging.getLogger("hackmail.secrets")
DEFAULT_PATH = "/run/secrets/hackmail_accounts"
_KEY = re.compile(r"^ACCOUNT_(\d+)_(EMAIL|NAME|PASSWORD)$")


def parse_accounts(text: str) -> dict[str, dict]:
    """Return {email: {"password": ..., "name": ...}}. Invalid entries are skipped; messages never include secrets."""
    raw: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        match = _KEY.match(key.strip())
        if not match:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        raw.setdefault(int(match.group(1)), {})[match.group(2)] = value

    accounts: dict[str, dict] = {}
    for index in sorted(raw):
        entry = raw[index]
        email = entry.get("EMAIL", "")
        password = re.sub(r"\s+", "", entry.get("PASSWORD", ""))  # app passwords are shown with spaces
        if not is_valid_email(email):
            log.warning("Accounts file: ACCOUNT_%d has no valid EMAIL; skipped.", index)
        elif not password:
            log.warning("Accounts file: ACCOUNT_%d has no PASSWORD; skipped.", index)
        elif imapx.IMAP_HOST == "imap.gmail.com" and len(password) != 16:
            log.warning("Accounts file: ACCOUNT_%d PASSWORD is not 16 characters; skipped.", index)
        elif email.lower() in {e.lower() for e in accounts}:
            log.warning("Accounts file: ACCOUNT_%d repeats an earlier address; skipped.", index)
        else:
            accounts[email] = {"password": password, "name": entry.get("NAME", "").strip()[:100]}
    return accounts


def load_accounts(path: str | None = None) -> dict[str, dict]:
    location = Path(path or os.environ.get("HACKMAIL_ACCOUNTS_FILE", DEFAULT_PATH))
    try:
        info = location.stat()
        text = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as error:
        log.warning("Could not read the accounts file (%s).", type(error).__name__)
        return {}
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("The accounts file is readable by other users on this machine; run: chmod 600 .accounts")
    accounts = parse_accounts(text)
    log.info("Loaded %d configured account(s) from the accounts file.", len(accounts))
    return accounts


# ---------------------------------------------------------------- opt-in writes ("Remember this login")

import fcntl  # noqa: E402  (POSIX only; the container is Linux)
import threading  # noqa: E402

_write_lock = threading.Lock()
_ENTRY_LINE = re.compile(r"^\s*ACCOUNT_(\d+)_(EMAIL|NAME|PASSWORD)\s*=(.*)$")


class AccountsFileError(Exception):
    """A save/forget failure whose message is safe to show to the user (never contains secrets)."""


def _clean(value: str, label: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AccountsFileError(f"The {label} cannot contain line breaks or control characters.")
    return value.strip()


def _rewrite(path: Path, transform) -> None:
    """Read the file, apply `transform(lines) -> lines`, and write it back IN PLACE.

    The file is a single-file bind mount, so it must be modified in place (a rename would replace the inode and the host
    would stop seeing our changes). The mode and owner are left untouched.
    """
    try:
        with path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                lines = handle.read().splitlines()
                updated = transform(lines)
                handle.seek(0)
                handle.write("\n".join(updated).rstrip("\n") + "\n")
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except FileNotFoundError:
        raise AccountsFileError("The accounts file does not exist. Create .accounts next to docker-compose.yml first.") from None
    except PermissionError:
        raise AccountsFileError("The accounts file is not writable by the app (check that it is owned by your user and mounted read-write).") from None
    except OSError as error:
        raise AccountsFileError(f"The accounts file could not be updated ({type(error).__name__}).") from None


def _indexes_for(lines: list[str], email: str) -> set[int]:
    """Entry numbers whose EMAIL matches (case-insensitive)."""
    found = set()
    for line in lines:
        match = _ENTRY_LINE.match(line)
        if match and match.group(2) == "EMAIL" and match.group(3).strip().strip("\"'").lower() == email.lower():
            found.add(int(match.group(1)))
    return found


def save_account(path: str | None, email: str, name: str, password: str) -> None:
    """Add the account to the file, or update the password/name of an existing entry for the same address."""
    email = _clean(email, "email address")
    name = _clean(name, "name")
    password = re.sub(r"\s+", "", _clean(password, "password"))
    location = Path(path or os.environ.get("HACKMAIL_ACCOUNTS_FILE", DEFAULT_PATH))

    def transform(lines: list[str]) -> list[str]:
        existing = _indexes_for(lines, email)
        if existing:
            keep = min(existing)
            out, seen = [], set()
            for line in lines:
                match = _ENTRY_LINE.match(line)
                if match and int(match.group(1)) in existing:
                    index, key = int(match.group(1)), match.group(2)
                    if index != keep:
                        continue  # drop duplicate entries for the same address
                    seen.add(key)
                    if key == "PASSWORD":
                        line = f"ACCOUNT_{index}_PASSWORD={password}"
                    elif key == "NAME" and name:
                        line = f"ACCOUNT_{index}_NAME={name}"
                out.append(line)
            for key, value in (("NAME", name), ("PASSWORD", password)):
                if key not in seen and value:
                    out.append(f"ACCOUNT_{keep}_{key}={value}")
            return out
        numbers = [int(m.group(1)) for m in map(_ENTRY_LINE.match, lines) if m]
        # Also reserve numbers used by commented-out template lines so we never reuse #ACCOUNT_2_* confusingly.
        nxt = max(numbers, default=0) + 1
        block = [f"ACCOUNT_{nxt}_EMAIL={email}"]
        if name:
            block.append(f"ACCOUNT_{nxt}_NAME={name}")
        block.append(f"ACCOUNT_{nxt}_PASSWORD={password}")
        return [*lines, *([""] if lines and lines[-1].strip() else []), *block]

    with _write_lock:
        _rewrite(location, transform)


def forget_account(path: str | None, email: str) -> bool:
    """Remove every entry for `email`. Returns True if anything was removed."""
    location = Path(path or os.environ.get("HACKMAIL_ACCOUNTS_FILE", DEFAULT_PATH))
    removed = False

    def transform(lines: list[str]) -> list[str]:
        nonlocal removed
        indexes = _indexes_for(lines, email)
        if not indexes:
            return lines
        removed = True
        return [line for line in lines if not ((m := _ENTRY_LINE.match(line)) and int(m.group(1)) in indexes)]

    with _write_lock:
        _rewrite(location, transform)
    return removed
