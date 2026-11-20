"""Online-only Gmail IMAP helpers. Nothing is cached or written to disk; every operation opens its own connection."""
import base64
import imaplib
import os
import quopri
import re
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import Callable

from .utf7 import decode_mutf7

IMAP_HOST = os.environ.get("HACKMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.environ.get("HACKMAIL_IMAP_PORT", "993"))
IMAP_TLS = os.environ.get("HACKMAIL_IMAP_TLS", "1") != "0"
TIMEOUT = 30
LIST_COUNT = 50
MAX_TEXT_PART_BYTES = 8 * 1024 * 1024
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_INLINE_IMAGE_BYTES = 1024 * 1024
MAX_INLINE_TOTAL_BYTES = 4 * 1024 * 1024
HEADER_FIELDS = "FROM TO CC SUBJECT DATE MESSAGE-ID REFERENCES IN-REPLY-TO REPLY-TO"

Progress = Callable[[int, int, str], None]
TOTAL_STEPS = 6


class MailError(Exception):
    """An error whose message is safe to show to the user (never contains credentials)."""


def quote(value: str) -> str:
    """Quote a user/server-derived value for an IMAP command; reject control characters."""
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MailError("That value contains characters that are not allowed.")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Job:
    """Carries progress reporting and cancellation for one operation."""

    def __init__(self, progress: Progress | None = None):
        self._progress = progress
        self.conn: imaplib.IMAP4 | None = None
        self.cancelled = False

    def step(self, number: int, text: str, total: int = TOTAL_STEPS) -> None:
        if self._progress:
            self._progress(number, total, text)

    def abort(self) -> None:
        self.cancelled = True
        conn = self.conn
        if conn is not None:
            try:
                conn.socket().shutdown(2)
            except Exception:
                pass


def connect(job: Job, email: str, password: str) -> imaplib.IMAP4:
    job.step(1, "Connecting securely to Gmail…")
    try:
        conn: imaplib.IMAP4 = (
            imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=TIMEOUT) if IMAP_TLS
            else imaplib.IMAP4(IMAP_HOST, IMAP_PORT, timeout=TIMEOUT)
        )
    except (OSError, imaplib.IMAP4.error) as error:
        raise MailError(f"Could not connect to the mail server ({type(error).__name__}).") from None
    job.conn = conn
    job.step(2, "Signing in…")
    try:
        conn.login(email, password)
    except imaplib.IMAP4.error as error:
        text = str(error)
        if "AUTHENTICATIONFAILED" in text.upper() or "invalid credentials" in text.lower() or "AUTH" in text.upper():
            raise MailError("Gmail rejected the email address or app password.") from None
        raise MailError("Sign-in failed.") from None
    return conn


def disconnect(job: Job, conn: imaplib.IMAP4 | None, step: bool = True) -> None:
    if conn is None:
        return
    if step:
        job.step(6, "Signing out…")
    try:
        conn.logout()
    except Exception:
        try:
            conn.shutdown()
        except Exception:
            pass


def _check(result: tuple, what: str) -> list:
    status, data = result
    if status != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode("utf-8", "replace")[:200]
        raise MailError(f"{what} failed: {detail or status}")
    return data


# ---------------------------------------------------------------- folders

_LIST_RE = re.compile(r'\((?P<attrs>[^)]*)\) (?P<delim>"[^"]*"|NIL) (?P<name>.+)$')


def list_folders(conn: imaplib.IMAP4) -> list[dict]:
    data = _check(conn.list(), "Listing folders")
    folders = []
    for item in data:
        if isinstance(item, tuple):
            item = item[0] + b" " + b'"' + item[1] + b'"'
        if not isinstance(item, bytes):
            continue
        match = _LIST_RE.match(item.decode("utf-8", "replace"))
        if not match:
            continue
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        attrs = [a for a in match.group("attrs").split() if a]
        lowered = {a.lower() for a in attrs}
        if "\\noselect" in lowered or "\\nonexistent" in lowered:
            continue
        display = decode_mutf7(name.split("/")[-1])
        if name.upper() == "INBOX":
            display = "Inbox"
        folders.append({
            "path": name,
            "name": display,
            "attrs": attrs,
            "inbox": name.upper() == "INBOX",
            "trash": "\\trash" in lowered,
            "spam": "\\junk" in lowered,
            "sent": "\\sent" in lowered,
            "drafts": "\\drafts" in lowered,
            "flagged": "\\flagged" in lowered,
            "all": "\\all" in lowered,
        })
    return folders


# ---------------------------------------------------------------- message lists

_UID_RE = re.compile(r"UID (\d+)")
_FLAGS_RE = re.compile(r"FLAGS \(([^)]*)\)")


def _address_display(value: str) -> tuple[str, str]:
    from email.utils import parseaddr
    name, address = parseaddr(value or "")
    return name or address, address


def _parse_header_block(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)


def _message_summary(record: dict) -> dict | None:
    text = record["text"]
    uid_match = _UID_RE.search(text)
    if not uid_match:
        return None
    flags = {f.lower() for f in (_FLAGS_RE.search(text).group(1).split() if _FLAGS_RE.search(text) else [])}
    headers = _parse_header_block(record.get("header", b""))
    sender, sender_email = _address_display(str(headers.get("From", "")))
    date_iso = ""
    try:
        parsed = parsedate_to_datetime(str(headers.get("Date", "")))
        date_iso = parsed.astimezone().isoformat() if parsed.tzinfo else parsed.isoformat()
    except Exception:
        pass
    return {
        "uid": int(uid_match.group(1)),
        "sender": sender or "(Unknown sender)",
        "sender_email": sender_email,
        "subject": str(headers.get("Subject", "")).replace("\r", " ").replace("\n", " ").strip() or "(No Subject)",
        "date": date_iso,
        "unread": "\\seen" not in flags,
        "flagged": "\\flagged" in flags,
        "has_attachment": bool(re.search(r'"(?:attachment)"', text, re.I)),
    }


def _fetch_records(data: list) -> list[dict]:
    records: list[dict] = []
    for item in data:
        if isinstance(item, tuple):
            prefix = item[0].decode("utf-8", "replace")
            if re.match(r"^\d+ \(", prefix):
                records.append({"text": prefix, "header": item[1]})
            elif records:
                records[-1]["text"] += prefix
        elif isinstance(item, bytes) and records:
            records[-1]["text"] += " " + item.decode("utf-8", "replace")
    return records


def list_messages(job: Job, email: str, password: str, folder: str, query: str | None = None) -> dict:
    conn = None
    try:
        conn = connect(job, email, password)
        job.step(3, "Opening the folder…" if not query else "Opening the folder…")
        status, data = conn.select(quote(folder), readonly=True)
        if status != "OK":
            raise MailError("Could not open that folder.")
        total = int(data[0] or 0)
        fetch_spec = f"(UID FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
        messages: list[dict] = []
        matched = None
        if query:
            job.step(4, "Gmail is searching this folder (large folders can take a while)…")
            if not query.isascii():
                conn._encoding = "utf-8"  # noqa: SLF001 - imaplib needs this for non-ASCII search terms
            uids = _check(conn.uid("SEARCH", "X-GM-RAW", quote(query)), "Searching")[0].split()
            matched = len(uids)
            wanted = uids[-LIST_COUNT:]
            job.step(5, "Fetching the first matching messages…")
            if wanted:
                data = _check(conn.uid("FETCH", b",".join(wanted).decode(), fetch_spec), "Fetching messages")
                messages = [m for m in map(_message_summary, _fetch_records(data)) if m]
        elif total > 0:
            job.step(4, "Reading the folder…")
            low = max(1, total - LIST_COUNT + 1)
            job.step(5, "Fetching the newest messages…")
            data = _check(conn.fetch(f"{low}:{total}", fetch_spec), "Fetching messages")
            messages = [m for m in map(_message_summary, _fetch_records(data)) if m]
        messages.sort(key=lambda m: (m["date"], m["uid"]), reverse=True)
        return {"messages": messages, "total": total if not query else matched, "query": query}
    finally:
        disconnect(job, conn)


# ---------------------------------------------------------------- BODYSTRUCTURE

def _tokenize(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \r\n\t":
            i += 1
        elif c in "()":
            yield c
            i += 1
        elif c == '"':
            i += 1
            out = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                out.append(text[i])
                i += 1
            i += 1
            yield ("s", "".join(out))
        else:
            j = i
            while j < n and text[j] not in " ()\r\n\t":
                j += 1
            word = text[i:j]
            i = j
            yield None if word.upper() == "NIL" else ("a", word)


def parse_sexp(text: str):
    """Parse one parenthesized IMAP list into nested Python lists (strings, None, ints stay as strings)."""
    tokens = list(_tokenize(text))
    pos = 0

    def parse():
        nonlocal pos
        token = tokens[pos]
        if token == "(":
            pos += 1
            items = []
            while pos < len(tokens) and tokens[pos] != ")":
                items.append(parse())
            pos += 1
            return items
        pos += 1
        return None if token is None else token[1]

    return parse()


def _params(value) -> dict:
    if not isinstance(value, list):
        return {}
    return {str(value[i]).lower(): str(value[i + 1]) for i in range(0, len(value) - 1, 2)}


def flatten_parts(node, prefix: str = "") -> list[dict]:
    """Walk a parsed BODYSTRUCTURE into a flat list of leaf parts with IMAP part numbers."""
    if not isinstance(node, list) or not node:
        return []
    if isinstance(node[0], list):  # multipart
        parts = []
        index = 0
        for child in node:
            if not isinstance(child, list):
                break
            index += 1
            parts.extend(flatten_parts(child, f"{prefix}.{index}" if prefix else str(index)))
        return parts
    number = prefix or "1"
    disposition = node[8] if len(node) > 8 and isinstance(node[8], list) else None
    # Single-part text parts carry a line count before md5; message/rfc822 parts add more fields. Disposition position:
    if isinstance(node[0], str) and node[0].lower() == "text":
        disposition = node[9] if len(node) > 9 and isinstance(node[9], list) else None
    elif isinstance(node[0], str) and node[0].lower() == "message":
        disposition = node[11] if len(node) > 11 and isinstance(node[11], list) else None
    disp_type = str(disposition[0]).lower() if disposition and disposition[0] else ""
    disp_params = _params(disposition[1]) if disposition and len(disposition) > 1 else {}
    params = _params(node[2])
    filename = disp_params.get("filename") or params.get("name") or ""
    return [{
        "number": number,
        "type": str(node[0]).lower(),
        "subtype": str(node[1]).lower(),
        "params": params,
        "cid": (str(node[3]).strip("<>") if len(node) > 3 and node[3] else ""),
        "encoding": str(node[5]).lower() if len(node) > 5 and node[5] else "7bit",
        "size": int(node[6]) if len(node) > 6 and str(node[6]).isdigit() else 0,
        "disposition": disp_type,
        "filename": decode_header_text(filename),
    }]


def decode_header_text(value: str) -> str:
    if not value:
        return ""
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def decode_part(raw: bytes, encoding: str) -> bytes:
    if encoding == "base64":
        return base64.b64decode(raw + b"=" * (-len(raw) % 4), validate=False)
    if encoding == "quoted-printable":
        return quopri.decodestring(raw)
    return raw


def decode_text(raw: bytes, charset: str | None) -> str:
    for candidate in (charset or "utf-8", "utf-8", "windows-1252"):
        try:
            return raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _fetch_bytes(conn, uid: int, section: str) -> bytes:
    data = _check(conn.uid("FETCH", str(uid), f"(BODY.PEEK[{section}])"), "Fetching message data")
    for item in data:
        if isinstance(item, tuple):
            return item[1]
    return b""


def _fetch_structure(conn, uid: int):
    data = _check(conn.uid("FETCH", str(uid), "(FLAGS BODYSTRUCTURE)"), "Reading the message structure")
    text = " ".join(
        (d[0] + d[1] if isinstance(d, tuple) else d).decode("utf-8", "replace") for d in data if d
    )
    flags_match = _FLAGS_RE.search(text)
    flags = {f.lower() for f in flags_match.group(1).split()} if flags_match else set()
    start = text.upper().find("BODYSTRUCTURE ")
    if start < 0:
        raise MailError("The server did not return the message structure.")
    return parse_sexp(text[start + len("BODYSTRUCTURE "):]), flags


def _addresses(value) -> list[dict]:
    if value is None:
        return []
    out = []
    for address in getattr(value, "addresses", []):
        email = f"{address.username}@{address.domain}" if address.username and address.domain else ""
        if email:
            out.append({"name": address.display_name or "", "email": email})
    return out


def read_message(job: Job, email: str, password: str, folder: str, uid: int) -> dict:
    """Fetch one message's headers, body, inline images, and attachment list (attachment bytes are NOT fetched)."""
    conn = None
    try:
        conn = connect(job, email, password)
        job.step(3, "Opening the folder…")
        status, _ = conn.select(quote(folder))
        if status != "OK":
            raise MailError("Could not open that folder.")
        job.step(4, "Reading the message structure…")
        structure, flags = _fetch_structure(conn, uid)
        parts = flatten_parts(structure)
        headers = _parse_header_block(_fetch_bytes(conn, uid, f"HEADER.FIELDS ({HEADER_FIELDS})"))

        def pick(subtype: str):
            for part in parts:
                if part["type"] == "text" and part["subtype"] == subtype and part["disposition"] != "attachment":
                    return part
            return None

        chosen = pick("html") or pick("plain")
        job.step(5, "Fetching the message text…")
        body_html, body_text, partial = None, None, False
        if chosen:
            if chosen["size"] > MAX_TEXT_PART_BYTES:
                partial = True
            raw = decode_part(_fetch_bytes(conn, uid, f"{chosen['number']}"), chosen["encoding"])[:MAX_TEXT_PART_BYTES]
            text = decode_text(raw, chosen["params"].get("charset"))
            if chosen["subtype"] == "html":
                body_html = text
            else:
                body_text = text
        # Inline images referenced by cid: come only from this message's own parts, with size caps.
        inline: dict[str, str] = {}
        if body_html:
            referenced = {c.lower() for c in re.findall(r"cid:([^\"'\s>)]+)", body_html, re.I)}
            budget = MAX_INLINE_TOTAL_BYTES
            for part in parts:
                cid = part["cid"].lower()
                if part["type"] == "image" and cid and cid in referenced and part["size"] <= MAX_INLINE_IMAGE_BYTES and budget > 0:
                    if part["subtype"] not in {"png", "jpeg", "jpg", "gif", "webp"}:
                        continue
                    data = decode_part(_fetch_bytes(conn, uid, part["number"]), part["encoding"])
                    budget -= len(data)
                    if budget >= 0:
                        inline[cid] = f"data:image/{part['subtype']};base64," + base64.b64encode(data).decode()
        used_cids = set(inline)
        attachments = []
        for part in parts:
            if part is chosen:
                continue
            is_inline_image = part["cid"].lower() in used_cids
            if is_inline_image:
                continue
            if part["disposition"] == "attachment" or part["filename"] or (part["type"] not in ("text", "multipart") and part["disposition"] != "inline"):
                if part["type"] == "text" and part["subtype"] in ("html", "plain") and not part["filename"] and part["disposition"] != "attachment":
                    continue
                attachments.append({
                    "part": part["number"],
                    "filename": part["filename"] or f"attachment-{part['number']}",
                    "type": f"{part['type']}/{part['subtype']}",
                    "size": part["size"],
                    "too_large": _decoded_size(part) > MAX_ATTACHMENT_BYTES,
                })
        if "\\seen" not in flags:
            job.step(5, "Marking the message as read…")
            conn.uid("STORE", str(uid), "+FLAGS.SILENT", "(\\Seen)")
        return {
            "uid": uid,
            "from": _addresses(headers.get("From"))[:1] or [{"name": "", "email": ""}],
            "to": _addresses(headers.get("To")),
            "cc": _addresses(headers.get("Cc")),
            "reply_to": _addresses(headers.get("Reply-To")),
            "subject": str(headers.get("Subject", "")).replace("\r", " ").replace("\n", " ").strip() or "(No Subject)",
            "date": str(headers.get("Date", "")),
            "message_id": str(headers.get("Message-ID", "")),
            "references": str(headers.get("References", "")),
            "html": body_html,
            "text": body_text,
            "inline": inline,
            "attachments": attachments,
            "partial": partial,
            "was_unread": "\\seen" not in flags,
            "flagged": "\\flagged" in flags,
        }
    finally:
        disconnect(job, conn)


def _decoded_size(part: dict) -> int:
    size = part["size"]
    return size * 3 // 4 if part["encoding"] == "base64" else size


def fetch_attachment(job: Job, email: str, password: str, folder: str, uid: int, part_number: str) -> tuple[bytes, dict]:
    if not re.fullmatch(r"\d+(\.\d+)*", part_number):
        raise MailError("Invalid attachment reference.")
    conn = None
    try:
        conn = connect(job, email, password)
        job.step(3, "Opening the folder…")
        if conn.select(quote(folder), readonly=True)[0] != "OK":
            raise MailError("Could not open that folder.")
        job.step(4, "Reading the message structure…")
        structure, _ = _fetch_structure(conn, uid)
        part = next((p for p in flatten_parts(structure) if p["number"] == part_number), None)
        if part is None:
            raise MailError("That attachment was not found.")
        if _decoded_size(part) > MAX_ATTACHMENT_BYTES:
            raise MailError("That attachment is larger than the 25 MiB limit.")
        job.step(5, "Downloading the attachment…")
        data = decode_part(_fetch_bytes(conn, uid, part_number), part["encoding"])
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise MailError("That attachment is larger than the 25 MiB limit.")
        return data, part
    finally:
        disconnect(job, conn)


# ---------------------------------------------------------------- actions

def _supports(conn, capability: str) -> bool:
    return capability in conn.capabilities


def _uid_set(uids: list[int]) -> str:
    unique = sorted({int(u) for u in uids if int(u) > 0})
    if not unique:
        raise MailError("No messages selected.")
    return ",".join(map(str, unique))


def move_uids(conn, uid_set: str, destination: str) -> None:
    if _supports(conn, "MOVE"):
        _check(conn.uid("MOVE", uid_set, quote(destination)), "Moving")
    else:
        _check(conn.uid("COPY", uid_set, quote(destination)), "Copying")
        _check(conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)"), "Marking deleted")
        if _supports(conn, "UIDPLUS"):
            _check(conn.uid("EXPUNGE", uid_set), "Removing")
        else:
            conn.expunge()


def run_action(job: Job, email: str, password: str, folder: str, uids: list[int], action: str,
               destination: str | None, trash_path: str | None, spam_path: str | None, inbox_path: str | None) -> dict:
    uid_set = _uid_set(uids)
    count = len(set(uids))
    noun = "the message" if count == 1 else f"{count} messages"
    conn = None
    try:
        conn = connect(job, email, password)
        job.step(3, "Opening the folder…")
        if conn.select(quote(folder))[0] != "OK":
            raise MailError("Could not open that folder.")
        removed = False
        if action in ("star", "unstar"):
            job.step(4, f"Updating {noun}…")
            sign = "+" if action == "star" else "-"
            _check(conn.uid("STORE", uid_set, f"{sign}FLAGS.SILENT", "(\\Flagged)"), "Updating")
        elif action in ("read", "unread"):
            job.step(4, f"Updating {noun}…")
            sign = "+" if action == "read" else "-"
            _check(conn.uid("STORE", uid_set, f"{sign}FLAGS.SILENT", "(\\Seen)"), "Updating")
        elif action == "move":
            if not destination:
                raise MailError("Choose a destination folder.")
            job.step(4, f"Moving {noun}…")
            move_uids(conn, uid_set, destination)
            removed = True
        elif action == "trash":
            if not trash_path:
                raise MailError("Gmail did not provide a Trash folder.")
            job.step(4, f"Moving {noun} to Trash…")
            move_uids(conn, uid_set, trash_path)
            removed = True
        elif action == "delete":
            job.step(4, f"Permanently deleting {noun}…")
            _check(conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)"), "Deleting")
            if _supports(conn, "UIDPLUS"):
                _check(conn.uid("EXPUNGE", uid_set), "Removing")
            else:
                conn.expunge()
            removed = True
        elif action == "spam":
            if not spam_path:
                raise MailError("Gmail did not provide a Spam folder.")
            job.step(4, f"Marking {noun} as spam…")
            move_uids(conn, uid_set, spam_path)
            removed = True
        elif action == "notspam":
            if not inbox_path:
                raise MailError("Could not find the Inbox.")
            job.step(4, f"Moving {noun} back to the Inbox…")
            move_uids(conn, uid_set, inbox_path)
            removed = True
        else:
            raise MailError("Unknown action.")
        job.step(5, "Waiting for Gmail to confirm…")
        return {"action": action, "count": count, "removed": removed}
    finally:
        disconnect(job, conn)


def inbox_state(job: Job, email: str, password: str, inbox_path: str = "INBOX") -> dict:
    conn = None
    try:
        conn = connect(job, email, password)
        status = _check(conn.status(quote(inbox_path), "(MESSAGES UNSEEN UIDNEXT)"), "Checking the Inbox")[0]
        text = status.decode("utf-8", "replace") if isinstance(status, bytes) else str(status)
        numbers = {k.upper(): int(v) for k, v in re.findall(r"(MESSAGES|UNSEEN|UIDNEXT) (\d+)", text)}
        return {"messages": numbers.get("MESSAGES", 0), "unseen": numbers.get("UNSEEN", 0), "uidnext": numbers.get("UIDNEXT", 0)}
    finally:
        disconnect(job, conn, step=False)


def new_subjects(job: Job, email: str, password: str, since_uid: int, limit: int = 5) -> list[str]:
    """Subjects only (never sender or body) of Inbox messages at or above `since_uid`, for notifications."""
    conn = None
    try:
        conn = connect(job, email, password)
        if conn.select("INBOX", readonly=True)[0] != "OK":
            return []
        data = _check(conn.uid("FETCH", f"{since_uid}:*", "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT)])"), "Fetching subjects")
        subjects = []
        for item in data:
            if isinstance(item, tuple) and re.search(rb"UID (\d+)", item[0]):
                uid = int(re.search(rb"UID (\d+)", item[0]).group(1))
                if uid >= since_uid:
                    subjects.append(str(_parse_header_block(item[1]).get("Subject", "")).strip() or "(No Subject)")
        return subjects[-limit:]
    finally:
        disconnect(job, conn, step=False)
