"""HackMail Web: a browser client for Gmail over IMAP/SMTP. Credentials and mail live in memory only."""
import asyncio
import json
import logging
import queue
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote as urlquote

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import compose, imapx, secrets_store
from .imapx import Job, MailError
from .sanitize import escape_text, sanitize_html

# Show our own INFO lines (e.g. how many accounts were loaded) next to uvicorn's logs. Passwords are never logged.
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")

STATIC = Path(__file__).resolve().parent.parent / "static"
SESSION_TTL = 12 * 3600
RENDER_TTL = 15 * 60
MAX_RENDERS = 200

app = FastAPI(title="HackMail Web", docs_url=None, redoc_url=None, openapi_url=None)


# ------------------------------------------------------------------ sessions (memory only)

CONFIGURED: dict[str, dict] = secrets_store.load_accounts()


class Session:
    def __init__(self):
        # email -> {"password": str, "name": str}; starts with the accounts from the optional secrets file.
        self.accounts: dict[str, dict] = {email: dict(info) for email, info in CONFIGURED.items()}
        self.renders: dict[str, tuple[float, str, bool]] = {}
        self.touched = time.time()


SESSIONS: dict[str, Session] = {}
_lock = threading.Lock()


def _purge() -> None:
    now = time.time()
    with _lock:
        for sid in [s for s, sess in SESSIONS.items() if now - sess.touched > SESSION_TTL]:
            SESSIONS.pop(sid, None)


def get_session(request: Request, create: bool = False) -> tuple[str, Session]:
    _purge()
    sid = request.cookies.get("hm_sid", "")
    with _lock:
        session = SESSIONS.get(sid)
        if session is None:
            if not create:
                raise HTTPException(401, "Sign in first.")
            sid = secrets.token_urlsafe(32)
            session = SESSIONS[sid] = Session()
        session.touched = time.time()
    return sid, session


def account_of(session: Session, email: str) -> dict:
    account = session.accounts.get(email)
    if account is None:
        raise HTTPException(401, "That account is not signed in. Add its app password again.")
    return account


# ------------------------------------------------------------------ security middleware

@app.middleware("http")
async def security(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        host = request.headers.get("host", "")
        if request.headers.get("x-hackmail") != "1" or (origin and urlparse_host(origin) != host):
            return JSONResponse({"detail": "Request rejected."}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    if not request.url.path.startswith("/render/"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; frame-src 'self'; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'",
        )
    return response


def urlparse_host(origin: str) -> str:
    return re.sub(r"^https?://", "", origin).split("/")[0]


# ------------------------------------------------------------------ streaming jobs

def stream_job(fn: Callable, *args) -> StreamingResponse:
    """Run blocking `fn(job, *args)` in a thread and stream NDJSON: progress lines, then a result or error line."""
    updates: queue.Queue = queue.Queue()
    job = Job(lambda step, total, text: updates.put({"step": step, "total": total, "text": text}))

    def run():
        try:
            updates.put({"done": True, "result": fn(job, *args)})
        except MailError as error:
            updates.put({"error": str(error)})
        except HTTPException as error:
            updates.put({"error": str(error.detail)})
        except Exception as error:  # never leak internals or credentials
            updates.put({"error": f"Unexpected error ({type(error).__name__})."})
        finally:
            updates.put(None)

    threading.Thread(target=run, daemon=True).start()

    async def generator():
        try:
            while True:
                item = await asyncio.to_thread(updates.get)
                if item is None:
                    break
                yield json.dumps(item) + "\n"
        finally:
            job.abort()  # client went away (Cancel / navigation): close the IMAP connection

    return StreamingResponse(generator(), media_type="application/x-ndjson")


# ------------------------------------------------------------------ accounts

@app.post("/api/login")
async def login(request: Request, response: Response):
    body = await request.json()
    email = str(body.get("email", "")).strip()
    password = re.sub(r"\s+", "", str(body.get("password", "")))  # app passwords are shown with spaces
    name = str(body.get("name", "")).strip()[:100]
    if not compose.is_valid_email(email):
        raise HTTPException(400, "Enter a valid email address.")
    if imapx.IMAP_HOST == "imap.gmail.com" and len(password) != 16:
        raise HTTPException(400, "Google app passwords are 16 characters. Spaces are removed automatically.")
    sid, session = get_session(request, create=True)

    def check(job: Job):
        conn = None
        try:
            conn = imapx.connect(job, email, password)
            return {"folders": imapx.list_folders(conn)}
        finally:
            imapx.disconnect(job, conn, step=False)

    job = Job()
    try:
        result = await asyncio.to_thread(check, job)
    except MailError as error:
        raise HTTPException(400, str(error)) from None
    session.accounts[email] = {"password": password, "name": name}
    response.set_cookie("hm_sid", sid, httponly=True, samesite="strict", max_age=SESSION_TTL, path="/")
    outcome: dict = {"email": email, "name": name, **result, "saved": False}
    if body.get("remember") is True:
        # Opt-in only: the user ticked "Remember this login". Sign-in already succeeded, so a failed save is reported
        # alongside it instead of failing the login.
        try:
            await asyncio.to_thread(secrets_store.save_account, None, email, name, password)
            CONFIGURED[email] = {"password": password, "name": name}
            outcome["saved"] = True
        except secrets_store.AccountsFileError as error:
            outcome["remember_error"] = str(error)
    outcome["configured"] = email in CONFIGURED
    return outcome


@app.post("/api/logout")
async def logout(request: Request):
    _, session = get_session(request)
    body = await request.json()
    email = str(body.get("email", ""))
    session.accounts.pop(email, None)
    result = {"ok": True, "forgotten": False}
    if body.get("forget") is True and email in CONFIGURED:
        try:
            await asyncio.to_thread(secrets_store.forget_account, None, email)
        except secrets_store.AccountsFileError as error:
            result["forget_error"] = str(error)
        else:
            CONFIGURED.pop(email, None)
            result["forgotten"] = True
    return result


@app.get("/api/accounts")
async def accounts(request: Request, response: Response):
    sid, session = get_session(request, create=True)
    response.set_cookie("hm_sid", sid, httponly=True, samesite="strict", max_age=SESSION_TTL, path="/")
    return {"accounts": [{"email": e, "name": a["name"], "configured": e in CONFIGURED} for e, a in session.accounts.items()]}


@app.get("/api/folders")
async def folders(request: Request, account: str):
    _, session = get_session(request)
    creds = account_of(session, account)

    def work(job: Job):
        conn = None
        try:
            conn = imapx.connect(job, account, creds["password"])
            return {"folders": imapx.list_folders(conn)}
        finally:
            imapx.disconnect(job, conn, step=False)

    return stream_job(work)


# ------------------------------------------------------------------ messages

@app.get("/api/messages")
async def messages(request: Request, account: str, folder: str, q: str = ""):
    _, session = get_session(request)
    creds = account_of(session, account)
    return stream_job(imapx.list_messages, account, creds["password"], folder, q.strip()[:500] or None)


def _store_render(session: Session, document: str, images: bool) -> str:
    now = time.time()
    for key in [k for k, (t, _, _) in session.renders.items() if now - t > RENDER_TTL]:
        session.renders.pop(key, None)
    while len(session.renders) >= MAX_RENDERS:
        session.renders.pop(min(session.renders, key=lambda k: session.renders[k][0]))
    token = secrets.token_urlsafe(24)
    session.renders[token] = (now, document, images)
    return token


def _read_for_reader(session: Session, account: str, creds: dict, folder: str, uid: int, images: bool):
    def work(job: Job):
        message = imapx.read_message(job, account, creds["password"], folder, uid)
        if message["html"]:
            cleaned = sanitize_html(message["html"], allow_remote_images=images, cid_data=message["inline"])
            body, blocked = cleaned.html, cleaned.blocked_images
            kind = "html"
        else:
            body = _plain_text_html(message["text"] or "")
            blocked, kind = 0, "text"
        token = _store_render(session, _render_document(body, images), images)
        message.pop("html"), message.pop("text"), message.pop("inline")
        return {**message, "render": token, "kind": kind, "blocked_images": blocked, "images_allowed": images}

    return work


def _plain_text_html(text: str) -> str:
    escaped = escape_text(text[:2_000_000])
    # Raw standalone URLs stay visible and clickable; the click handler in the app confirms the true destination.
    linked = re.sub(r"(https?://[^\s<>\"')\]]+)", r'<a href="\1">\1</a>', escaped)
    return f'<div style="white-space:pre-wrap">{linked}</div>'


def _render_document(body: str, images: bool) -> str:
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><base target=\"_blank\">"
            "<style>body{margin:0;padding:4px;color:#2b2217;font:15px/1.6 'Atkinson Hyperlegible',system-ui,sans-serif;"
            "overflow-wrap:anywhere}img{max-width:100%;height:auto}table{max-width:100%}"
            "a{color:#9e4b27}blockquote{border-left:2px solid #d4ccbd;margin-left:0;padding-left:14px}</style>"
            f"</head><body>{body}</body></html>")


@app.get("/api/message")
async def message(request: Request, account: str, folder: str, uid: int, images: int = 0):
    _, session = get_session(request)
    creds = account_of(session, account)
    return stream_job(_read_for_reader(session, account, creds, folder, uid, bool(images)))


@app.get("/render/{token}")
async def render(request: Request, token: str):
    _, session = get_session(request)
    entry = session.renders.get(token)
    if entry is None:
        raise HTTPException(404, "Expired.")
    image_sources = "img-src data: https: http:" if entry[2] else "img-src data:"
    csp = f"default-src 'none'; {image_sources}; style-src 'unsafe-inline'; font-src data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
    return HTMLResponse(entry[1], headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


@app.get("/api/attachment")
async def attachment(request: Request, account: str, folder: str, uid: int, part: str):
    _, session = get_session(request)
    creds = account_of(session, account)
    try:
        data, info = await asyncio.to_thread(imapx.fetch_attachment, Job(), account, creds["password"], folder, uid, part)
    except MailError as error:
        raise HTTPException(400, str(error)) from None
    filename = re.sub(r"[\r\n\"\\/]", "_", info["filename"] or f"attachment-{part}")
    return Response(
        data,
        media_type="application/octet-stream",  # never let the browser render or execute it
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{urlquote(filename)}"},
    )


@app.post("/api/action")
async def action(request: Request):
    body = await request.json()
    _, session = get_session(request)
    account = str(body.get("account", ""))
    creds = account_of(session, account)
    folders_ = body.get("folders", {})
    return stream_job(
        imapx.run_action, account, creds["password"], str(body.get("folder", "")),
        [int(u) for u in body.get("uids", [])], str(body.get("action", "")),
        body.get("destination"), folders_.get("trash"), folders_.get("spam"), folders_.get("inbox"),
    )


@app.get("/api/inbox-state")
async def inbox_state(request: Request, account: str, since_uid: int = 0):
    _, session = get_session(request)
    creds = account_of(session, account)

    def work(job: Job):
        state = imapx.inbox_state(job, account, creds["password"])
        if since_uid and state["uidnext"] > since_uid:
            state["subjects"] = imapx.new_subjects(job, account, creds["password"], since_uid)
        return state

    try:
        return await asyncio.to_thread(work, Job())
    except MailError as error:
        raise HTTPException(400, str(error)) from None


# ------------------------------------------------------------------ compose

@app.get("/api/compose-context")
async def compose_context(request: Request, account: str, folder: str, uid: int, mode: str, images: int = 0):
    if mode not in ("reply", "replyall", "forward"):
        raise HTTPException(400, "Unknown mode.")
    _, session = get_session(request)
    creds = account_of(session, account)
    own = set(session.accounts)

    def work(job: Job):
        message = imapx.read_message(job, account, creds["password"], folder, uid)
        quote_html, _ = compose.quote_blocks(message, mode, bool(images))
        to, cc = ([], []) if mode == "forward" else compose.reply_targets(message, own, mode)
        return {"to": to, "cc": cc, "subject": compose.subject_for(message["subject"], mode), "quote_html": quote_html,
                "title": {"reply": "Reply", "replyall": "Reply All", "forward": "Forward"}[mode]}

    return stream_job(work)


@app.post("/api/send")
async def send(
    request: Request,
    account: str = Form(...), to: str = Form(""), cc: str = Form(""), bcc: str = Form(""), subject: str = Form(""),
    html: str = Form(""), quote_mode: str = Form(""), quote_folder: str = Form(""), quote_uid: int = Form(0),
    quote_images: int = Form(0), files: list[UploadFile] = File(default=[]),
):
    _, session = get_session(request)
    creds = account_of(session, account)
    uploads: list[tuple[str, str, bytes]] = []
    total = 0
    for upload in files:
        data = await upload.read(compose.MAX_ATTACHMENT_TOTAL + 1)
        total += len(data)
        if total > compose.MAX_ATTACHMENT_TOTAL:
            raise HTTPException(413, "Attachments exceed the 25 MiB total limit.")
        uploads.append((re.sub(r"[\r\n\"\\/]", "_", upload.filename or "attachment"), upload.content_type or "", data))
    to_list, cc_list, bcc_list = (compose.parse_recipients(v) for v in (to, cc, bcc))
    envelope = compose.envelope_recipients(to_list, cc_list, bcc_list)
    if not envelope:
        raise HTTPException(400, "Add at least one recipient.")
    if len(envelope) > compose.MAX_RECIPIENTS:
        raise HTTPException(400, f"Gmail allows up to {compose.MAX_RECIPIENTS} recipients per message.")
    name = creds["name"]

    def work(job: Job):
        job.step(0, "Preparing message…", total=7)
        quote_html = quote_text = in_reply_to = references = ""
        if quote_mode in ("reply", "replyall", "forward") and quote_uid:
            original = imapx.read_message(Job(), account, creds["password"], quote_folder, quote_uid)
            quote_html, quote_text = compose.quote_blocks(original, quote_mode, bool(quote_images))
            if quote_mode != "forward":
                in_reply_to, references = original["message_id"], original["references"]
        seen_to = {a.lower() for a in to_list}
        cc_only = [a for a in cc_list if a.lower() not in seen_to]
        message = compose.build_message(
            sender=account, sender_name=name, to=to_list, cc=cc_only, subject=subject, html_body=html,
            quote_html=quote_html, quote_text=quote_text, attachments=uploads,
            in_reply_to=in_reply_to, references=references,
        )
        return compose.send_message(job, account, creds["password"], message, envelope)

    return stream_job(work)


# ------------------------------------------------------------------ static app

@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
