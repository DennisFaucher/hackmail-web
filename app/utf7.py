"""IMAP modified UTF-7 mailbox names (RFC 3501 section 5.1.3), decoded for display only."""
import base64
import re


def decode_mutf7(name: str) -> str:
    def repl(match: re.Match) -> str:
        chunk = match.group(1)
        if chunk == "":
            return "&"
        raw = chunk.replace(",", "/")
        raw += "=" * (-len(raw) % 4)
        try:
            return base64.b64decode(raw).decode("utf-16-be")
        except Exception:
            return match.group(0)

    return re.sub(r"&([^-]*)-", repl, name)
