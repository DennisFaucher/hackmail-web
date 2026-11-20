"""Recipients, reply targets, outgoing MIME, and SMTP submission."""
import base64
import os
import re
import smtplib
import ssl
import uuid
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime, formatdate, make_msgid
from datetime import datetime, timezone

from .imapx import Job, MailError
from .sanitize import escape_text, html_to_text, sanitize_html

SMTP_HOST = os.environ.get("HACKMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("HACKMAIL_SMTP_PORT", "465"))
SMTP_TLS = os.environ.get("HACKMAIL_SMTP_TLS", "ssl")  # "ssl" for Gmail; "none" only for the local dev server
MAX_RECIPIENTS = 100
MAX_ATTACHMENT_TOTAL = 25 * 1024 * 1024
MAX_INLINE_IMAGES_TOTAL = 4 * 1024 * 1024
ACCEPTANCE_TIMEOUT = 180
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value)) and len(value) <= 254 and "." in value.split("@")[-1]


def parse_recipients(text: str) -> list[str]:
    """Split on commas/semicolons; accepts `Name <a@b.c>` forms. Raises MailError on anything invalid."""
    from email.utils import getaddresses
    if any(ord(c) < 32 and c not in "\t" for c in text):
        raise MailError("Recipient lists cannot contain line breaks.")
    result = []
    for _, address in getaddresses([text.replace(";", ",")]):
        address = address.strip()
        if not address:
            continue
        if not is_valid_email(address):
            raise MailError(f"“{address}” is not a valid email address.")
        result.append(address)
    return result


def envelope_recipients(to: list[str], cc: list[str], bcc: list[str]) -> list[str]:
    seen, out = set(), []
    for address in [*to, *cc, *bcc]:
        key = address.lower()
        if key not in seen:
            seen.add(key)
            out.append(address)
    return out


def reply_targets(message: dict, own_addresses: set[str], mode: str) -> tuple[list[str], list[str]]:
    """Return (to, cc) for reply or replyall. Own accounts are removed and addresses de-duplicated."""
    own = {a.lower() for a in own_addresses}
    seen = set(own)

    def others(addresses: list[str]) -> list[str]:
        out = []
        for address in addresses:
            if address and address.lower() not in seen:
                seen.add(address.lower())
                out.append(address)
        return out

    sender = [a["email"] for a in (message.get("reply_to") or message.get("from") or [])][:1]
    if mode == "reply":
        to = others(sender)
        return (to or sender), []
    to = others(sender + [a["email"] for a in message.get("to", [])])
    cc = others([a["email"] for a in message.get("cc", [])])
    if not to and not cc:
        to = sender
    return to, cc


def subject_for(subject: str, mode: str) -> str:
    lowered = subject.lower()
    if mode == "forward":
        return subject if lowered.startswith("fwd:") else f"Fwd: {subject}"
    return subject if lowered.startswith("re:") else f"Re: {subject}"


def quote_blocks(message: dict, mode: str, allow_remote_images: bool) -> tuple[str, str]:
    """Build the read-only quoted original (HTML, text) on the server; the client never supplies it."""
    sender = message["from"][0]
    who = f"{sender['name']} <{sender['email']}>" if sender["name"] else sender["email"]
    if mode == "forward":
        header = (f"---------- Forwarded message ----------\nFrom: {who}\nDate: {message['date']}\n"
                  f"Subject: {message['subject']}")
    else:
        header = f"On {message['date']}, {sender['name'] or sender['email']} wrote:"
    if message.get("html"):
        cleaned = sanitize_html(message["html"], allow_remote_images=allow_remote_images,
                                cid_data={k: v for k, v in message.get("inline", {}).items()})
        body_html = cleaned.html
        body_text = html_to_text(message["html"], limit=200_000)
    else:
        body_text = (message.get("text") or "")[:200_000]
        body_html = f"<pre>{escape_text(body_text)}</pre>"
    header_html = escape_text(header).replace("\n", "<br>")
    html = (f"<br><br><div>{header_html}</div><blockquote style=\"border-left:2px solid #d4ccbd;margin-left:0;"
            f"padding-left:14px\">{body_html}</blockquote>")
    return html, f"\n\n{header}\n{body_text}"


_DATA_IMG = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*["\'])data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=\s]+)(["\'])', re.I)


def build_message(*, sender: str, sender_name: str, to: list[str], cc: list[str], subject: str, html_body: str,
                  quote_html: str, quote_text: str, attachments: list[tuple[str, str, bytes]],
                  in_reply_to: str = "", references: str = "") -> EmailMessage:
    """Build the outgoing message. Bcc recipients are never written as a header (envelope only)."""
    if any(c in subject for c in "\r\n"):
        raise MailError("The subject cannot contain line breaks.")
    inline_images: list[tuple[str, str, bytes]] = []
    total = 0

    def to_cid(match: re.Match) -> str:
        nonlocal total
        data = base64.b64decode(re.sub(r"\s+", "", match.group(3)))
        total += len(data)
        if total > MAX_INLINE_IMAGES_TOTAL:
            raise MailError("Pasted inline images exceed the 4 MiB total limit. Resize or remove an image.")
        cid = f"{uuid.uuid4().hex}@hackmail"
        inline_images.append((cid, match.group(2).lower().replace("jpg", "jpeg"), data))
        return f"{match.group(1)}cid:{cid}{match.group(4)}"

    user_html = _DATA_IMG.sub(to_cid, html_body)
    # Everything the user typed passes through the same sanitizer; cid: references to our own images are kept.
    cleaned = sanitize_html(user_html, cid_passthrough={cid for cid, _, _ in inline_images}).html
    plain = html_to_text(html_body, limit=200_000) + quote_text
    document = f"<!doctype html><html><body>{cleaned}{quote_html}</body></html>"

    message = EmailMessage(policy=SMTP_POLICY)
    message["From"] = f'"{sender_name.replace(chr(34), "")}" <{sender}>' if sender_name else sender
    message["To"] = ", ".join(to) if to else "undisclosed-recipients:;"
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = (references + " " + in_reply_to).strip()
    message.set_content(plain)
    message.add_alternative(document, subtype="html")
    html_part = message.get_payload()[1]
    for cid, subtype, data in inline_images:
        html_part.add_related(data, "image", subtype, cid=f"<{cid}>", disposition="inline")
    for filename, content_type, data in attachments:
        maintype, _, subtype = (content_type or "application/octet-stream").partition("/")
        message.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                               filename=filename)
    return message


def send_message(job: Job, email: str, password: str, message: EmailMessage, envelope: list[str]) -> dict:
    if not envelope:
        raise MailError("Add at least one recipient.")
    if len(envelope) > MAX_RECIPIENTS:
        raise MailError(f"Gmail allows up to {MAX_RECIPIENTS} recipients per message.")
    job.step(1, "Connecting securely to Gmail…", total=7)
    try:
        if SMTP_TLS == "ssl":
            smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30, context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
    except OSError as error:
        raise MailError(f"Could not connect to the mail server ({type(error).__name__}).") from None
    try:
        job.step(2, "Signing in…", total=7)
        try:
            if SMTP_TLS == "ssl":
                smtp.login(email, password)
        except smtplib.SMTPAuthenticationError:
            raise MailError("Gmail rejected the email address or app password.") from None
        job.step(3, f"Addressing message to {len(envelope)} recipient{'s' if len(envelope) != 1 else ''}…", total=7)
        job.step(4, "Encoding message and attachments…", total=7)
        payload = message.as_bytes()
        job.step(5, "Uploading message to Gmail…", total=7)
        smtp.sock.settimeout(ACCEPTANCE_TIMEOUT)
        try:
            smtp.send_message(message, from_addr=email, to_addrs=envelope)
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError):
            raise MailError("The connection dropped before Gmail confirmed delivery. Check your Sent folder before "
                            "trying again, to avoid sending a duplicate.") from None
        except smtplib.SMTPException as error:
            raise MailError(f"Gmail refused the message: {str(error)[:200]}") from None
        job.step(6, "Gmail accepted the message.", total=7)
        try:
            smtp.quit()
        except Exception:
            pass  # a QUIT failure after acceptance must not suggest a retry
        job.step(7, "Done.", total=7)
        return {"sent": True, "recipients": len(envelope), "bytes": len(payload)}
    finally:
        try:
            smtp.close()
        except Exception:
            pass
