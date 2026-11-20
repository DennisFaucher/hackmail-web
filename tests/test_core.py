import base64

import pytest

from app import compose, imapx
from app.sanitize import html_to_text, sanitize_html
from app.utf7 import decode_mutf7


def test_mutf7_decodes_display_names():
    assert decode_mutf7("&AOk-t&AOk-") == "été"
    assert decode_mutf7("Plain &- name") == "Plain & name"


def test_scripts_forms_and_event_handlers_are_removed():
    out = sanitize_html('<p onclick="x()">Hi</p><script>alert(1)</script><form action="/x"><input></form><iframe src="x"></iframe>').html
    for bad in ("script", "onclick", "<form", "<input", "iframe", "alert"):
        assert bad not in out
    assert "Hi" in out


def test_remote_images_blocked_by_default_and_counted():
    result = sanitize_html('<img src="https://t.example/p.gif"><img src="http://t.example/q.gif">')
    assert "t.example" not in result.html
    assert result.blocked_images == 2
    allowed = sanitize_html('<img src="https://t.example/p.gif">', allow_remote_images=True)
    assert "https://t.example/p.gif" in allowed.html and allowed.blocked_images == 0


def test_css_url_loads_are_dropped():
    out = sanitize_html('<div style="background:url(https://t.example/x.png);color:red;background-image:url(http://a/b)">x</div><style>@import url(http://evil)</style>').html
    assert "url(" not in out and "evil" not in out and "t.example" not in out
    assert "color: red" in out or "color:red" in out


def test_unsafe_link_schemes_removed_and_rel_set():
    out = sanitize_html('<a href="javascript:alert(1)">a</a><a href="data:text/html,x">b</a><a href="https://ok.example">c</a>').html
    assert "javascript" not in out and "data:text" not in out
    assert 'href="https://ok.example"' in out and "noopener" in out


def test_cid_images_resolve_only_from_the_messages_own_parts():
    data = {"abc@x": "data:image/png;base64,AAAA"}
    result = sanitize_html('<img src="cid:abc@x"><img src="cid:other@x">', cid_data=data)
    assert result.html.count("data:image/png;base64,AAAA") == 1
    assert "other@x" not in result.html


def test_data_images_survive_but_other_data_uris_do_not():
    ok = sanitize_html('<img src="data:image/png;base64,iVBORw0KGgo=">').html
    assert "data:image/png;base64,iVBORw0KGgo=" in ok
    assert "data:text" not in sanitize_html('<img src="data:text/html;base64,PHNjcmlwdD4=">').html


def test_html_to_text_is_bounded_and_readable():
    assert html_to_text("<p>Hello</p><p>World<br>again</p>") == "Hello\n\nWorld\nagain"
    assert len(html_to_text("<p>" + "x" * 5000 + "</p>", limit=100)) <= 200


def test_bodystructure_parsing_finds_parts_and_attachments():
    text = ('(("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 12 1 NIL NIL NIL NIL)'
            '("text" "html" ("charset" "utf-8") NIL NIL "quoted-printable" 55 2 NIL NIL NIL NIL)'
            '("application" "pdf" ("name" "r.pdf") NIL NIL "base64" 4000 NIL ("attachment" ("filename" "r.pdf")) NIL NIL) "mixed" ("boundary" "b") NIL NIL NIL)')
    parts = imapx.flatten_parts(imapx.parse_sexp(text))
    assert [p["number"] for p in parts] == ["1", "2", "3"]
    assert parts[1]["subtype"] == "html" and parts[1]["encoding"] == "quoted-printable"
    assert parts[2]["disposition"] == "attachment" and parts[2]["filename"] == "r.pdf"


def test_single_part_message_is_part_one():
    parts = imapx.flatten_parts(imapx.parse_sexp('("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 5 1 NIL NIL NIL NIL)'))
    assert parts[0]["number"] == "1"


def test_encoded_word_split_mid_word_has_no_stray_space():
    from email import policy
    from email.parser import BytesParser
    raw = b"Subject: =?UTF-8?Q?You=E2=80=99ve_reported_you_didn=E2=80=99t_rec?=\r\n =?UTF-8?Q?eive_your_item:?= Request 5\r\n\r\n"
    assert str(BytesParser(policy=policy.default).parsebytes(raw)["Subject"]) == "You’ve reported you didn’t receive your item: Request 5"


def test_quote_rejects_control_characters_and_escapes():
    assert imapx.quote('A "b"') == '"A \\"b\\""'
    with pytest.raises(imapx.MailError):
        imapx.quote("bad\r\nname")


def test_recipient_parsing_and_dedup():
    assert compose.parse_recipients("a@x.com; B <b@x.com>, A@X.com") == ["a@x.com", "b@x.com", "A@X.com"]
    assert compose.envelope_recipients(["a@x.com"], ["A@x.com", "c@x.com"], ["c@X.com"]) == ["a@x.com", "c@x.com"]
    with pytest.raises(imapx.MailError):
        compose.parse_recipients("not-an-address")
    with pytest.raises(imapx.MailError):
        compose.parse_recipients("a@x.com\r\nBcc: evil@x.com")


MESSAGE = {
    "from": [{"name": "Zoe", "email": "zoe@example.com"}],
    "to": [{"name": "", "email": "ME@example.test"}, {"name": "", "email": "bob@example.com"}, {"name": "", "email": "Zoe@example.com"}],
    "cc": [{"name": "", "email": "other@example.test"}, {"name": "", "email": "cara@example.com"}, {"name": "", "email": "BOB@example.com"}],
}


def test_reply_all_excludes_own_accounts_and_duplicates():
    to, cc = compose.reply_targets(MESSAGE, {"me@example.test", "other@example.test"}, "replyall")
    assert to == ["zoe@example.com", "bob@example.com"] and cc == ["cara@example.com"]


def test_reply_goes_to_reply_to_when_present_and_reply_all_falls_back_to_sender():
    assert compose.reply_targets({**MESSAGE, "reply_to": [{"name": "", "email": "list@example.com"}]}, set(), "reply")[0] == ["list@example.com"]
    mine = {"from": [{"name": "", "email": "me@example.test"}], "to": [{"name": "", "email": "me@example.test"}], "cc": []}
    assert compose.reply_targets(mine, {"me@example.test"}, "replyall") == (["me@example.test"], [])


def test_subjects():
    assert compose.subject_for("Plans", "reply") == "Re: Plans"
    assert compose.subject_for("RE: Plans", "replyall") == "RE: Plans"
    assert compose.subject_for("Plans", "forward") == "Fwd: Plans"


def _build(**kw):
    args = dict(sender="me@example.test", sender_name="Me", to=["a@x.com"], cc=[], subject="Hi", html_body="<p>Hello</p>",
                quote_html="", quote_text="", attachments=[])
    args.update(kw)
    return compose.build_message(**args)


def test_bcc_is_never_written_and_to_only_bcc_uses_undisclosed():
    message = _build(to=[])
    raw = message.as_string()
    assert "undisclosed-recipients:;" in raw and "Bcc" not in raw
    assert _build(cc=["c@x.com"])["Cc"] == "c@x.com"


def test_message_has_plain_and_html_alternatives_and_attachment():
    message = _build(attachments=[("a.txt", "text/plain", b"hi")])
    kinds = [part.get_content_type() for part in message.walk()]
    assert "text/plain" in kinds and "text/html" in kinds
    assert [p.get_filename() for p in message.iter_attachments()] == ["a.txt"]


def test_pasted_images_become_inline_cid_parts_and_scripts_are_stripped():
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    message = _build(html_body=f'<p>x</p><script>1</script><img src="data:image/png;base64,{png}">')
    html = message.get_body(("html",)).get_content()
    assert "cid:" in html and "data:image" not in html and "script" not in html
    assert any(p.get_content_type() == "image/png" for p in message.walk())


def test_subject_with_line_break_is_rejected():
    with pytest.raises(imapx.MailError):
        _build(subject="Hi\r\nBcc: x@y.z")


def test_quote_is_built_on_the_server_and_sanitized():
    original = {"from": [{"name": "Zoe", "email": "z@x.com"}], "date": "Fri, 6 Nov 2026", "subject": "S",
                "html": '<p>Hi</p><img src="https://t.example/p.gif"><script>x</script>', "text": None, "inline": {}}
    html, text = compose.quote_blocks(original, "reply", False)
    assert "On Fri, 6 Nov 2026, Zoe wrote:" in html and "t.example" not in html and "script" not in html
    assert "Hi" in text


# ---------------------------------------------------------------- secrets file

from app import secrets_store  # noqa: E402


def test_accounts_file_parses_numbered_accounts_and_normalizes_passwords():
    text = '''# comment
ACCOUNT_1_EMAIL=me@gmail.com
ACCOUNT_1_NAME=Me Myself
ACCOUNT_1_PASSWORD=abcd efgh ijkl mnop

ACCOUNT_2_EMAIL="two@gmail.com"
ACCOUNT_2_PASSWORD='wxyzwxyzwxyzwxyz'
'''
    accounts = secrets_store.parse_accounts(text)
    assert accounts == {
        "me@gmail.com": {"password": "abcdefghijklmnop", "name": "Me Myself"},
        "two@gmail.com": {"password": "wxyzwxyzwxyzwxyz", "name": ""},
    }


def test_accounts_file_skips_invalid_entries_without_leaking_secrets(caplog):
    text = '''ACCOUNT_1_EMAIL=not-an-email
ACCOUNT_1_PASSWORD=abcdefghijklmnop
ACCOUNT_2_EMAIL=ok@gmail.com
ACCOUNT_2_PASSWORD=tooshort
ACCOUNT_3_EMAIL=dup@gmail.com
ACCOUNT_3_PASSWORD=abcdefghijklmnop
ACCOUNT_4_EMAIL=DUP@gmail.com
ACCOUNT_4_PASSWORD=abcdefghijklmnop
ACCOUNT_5_EMAIL=nopass@gmail.com
'''
    with caplog.at_level("WARNING"):
        accounts = secrets_store.parse_accounts(text)
    assert list(accounts) == ["dup@gmail.com"]
    logged = caplog.text
    assert "abcdefghijklmnop" not in logged and "tooshort" not in logged and "gmail.com" not in logged


def test_missing_or_empty_accounts_file_means_no_accounts(tmp_path):
    assert secrets_store.load_accounts(str(tmp_path / "missing")) == {}
    empty = tmp_path / ".accounts"
    empty.write_text("# nothing yet\n")
    empty.chmod(0o600)
    assert secrets_store.load_accounts(str(empty)) == {}


def test_configured_accounts_preload_every_new_session_but_never_expose_passwords(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(main, "CONFIGURED", {"me@gmail.com": {"password": "abcdefghijklmnop", "name": "Me"}})
    client = TestClient(main.app)
    response = client.get("/api/accounts")
    assert response.json() == {"accounts": [{"email": "me@gmail.com", "name": "Me", "configured": True}]}
    assert "abcdefghijklmnop" not in response.text
    assert "hm_sid" in response.headers.get("set-cookie", "") and "HttpOnly" in response.headers["set-cookie"]
    # Signing out of a configured account only affects this session.
    client.post("/api/logout", json={"email": "me@gmail.com"}, headers={"X-HackMail": "1"})
    assert client.get("/api/accounts").json()["accounts"] == []
    assert TestClient(main.app).get("/api/accounts").json()["accounts"][0]["email"] == "me@gmail.com"


# ---------------------------------------------------------------- "Remember this login" writes

def _accounts_file(tmp_path, text="# my accounts\n"):
    path = tmp_path / ".accounts"
    path.write_text(text)
    path.chmod(0o600)
    return path


def test_save_appends_a_numbered_entry_and_keeps_comments_mode_and_inode(tmp_path):
    path = _accounts_file(tmp_path, "# my accounts\nACCOUNT_1_EMAIL=a@gmail.com\nACCOUNT_1_PASSWORD=aaaabbbbccccdddd\n")
    inode = path.stat().st_ino
    secrets_store.save_account(str(path), "b@gmail.com", "Bee", "eeee ffff gggg hhhh")
    text = path.read_text()
    assert text.startswith("# my accounts\n") and "ACCOUNT_2_EMAIL=b@gmail.com" in text and "ACCOUNT_2_NAME=Bee" in text
    assert "ACCOUNT_2_PASSWORD=eeeeffffgggghhhh" in text  # spaces removed
    assert path.stat().st_ino == inode and (path.stat().st_mode & 0o777) == 0o600  # in place, mode untouched
    assert secrets_store.parse_accounts(text) == {
        "a@gmail.com": {"password": "aaaabbbbccccdddd", "name": ""},
        "b@gmail.com": {"password": "eeeeffffgggghhhh", "name": "Bee"},
    }


def test_save_for_an_existing_address_updates_instead_of_duplicating(tmp_path):
    path = _accounts_file(tmp_path, "ACCOUNT_1_EMAIL=a@gmail.com\nACCOUNT_1_NAME=Old\nACCOUNT_1_PASSWORD=aaaabbbbccccdddd\n")
    secrets_store.save_account(str(path), "A@Gmail.com", "New Name", "1111222233334444")
    accounts = secrets_store.parse_accounts(path.read_text())
    assert list(accounts) == ["a@gmail.com"] and accounts["a@gmail.com"] == {"password": "1111222233334444", "name": "New Name"}
    assert path.read_text().count("ACCOUNT_1_EMAIL") == 1


def test_forget_removes_only_that_account_and_keeps_the_rest(tmp_path):
    path = _accounts_file(tmp_path, "# keep me\nACCOUNT_1_EMAIL=a@gmail.com\nACCOUNT_1_PASSWORD=aaaabbbbccccdddd\n\nACCOUNT_2_EMAIL=b@gmail.com\nACCOUNT_2_PASSWORD=eeeeffffgggghhhh\n")
    assert secrets_store.forget_account(str(path), "A@gmail.com") is True
    assert secrets_store.forget_account(str(path), "nobody@gmail.com") is False
    text = path.read_text()
    assert "# keep me" in text and "a@gmail.com" not in text and "aaaabbbbccccdddd" not in text
    assert list(secrets_store.parse_accounts(text)) == ["b@gmail.com"]


def test_save_rejects_control_characters_and_reports_unwritable_or_missing_files(tmp_path):
    path = _accounts_file(tmp_path)
    with pytest.raises(secrets_store.AccountsFileError):
        secrets_store.save_account(str(path), "a@gmail.com", "Bad\nACCOUNT_9_EMAIL=evil@x.com", "aaaabbbbccccdddd")
    with pytest.raises(secrets_store.AccountsFileError):
        secrets_store.save_account(str(path), "a@gmail.com", "", "pass\r\nword")
    assert path.read_text() == "# my accounts\n"
    with pytest.raises(secrets_store.AccountsFileError) as missing:
        secrets_store.save_account(str(tmp_path / "nope"), "a@gmail.com", "", "aaaabbbbccccdddd")
    assert "aaaabbbbccccdddd" not in str(missing.value)


def _patched_client(monkeypatch, tmp_path, configured=None):
    from fastapi.testclient import TestClient
    from app import main
    path = _accounts_file(tmp_path)
    monkeypatch.setenv("HACKMAIL_ACCOUNTS_FILE", str(path))
    monkeypatch.setattr(main, "CONFIGURED", dict(configured or {}))
    monkeypatch.setattr(main.imapx, "connect", lambda job, email, password: object())
    monkeypatch.setattr(main.imapx, "list_folders", lambda conn: [{"path": "INBOX", "name": "Inbox", "inbox": True}])
    monkeypatch.setattr(main.imapx, "disconnect", lambda *a, **k: None)
    return TestClient(main.app), main, path


LOGIN = {"email": "me@gmail.com", "password": "abcd efgh ijkl mnop", "name": "Me"}
HEADERS = {"X-HackMail": "1"}


def test_login_without_remember_never_touches_the_file(monkeypatch, tmp_path):
    client, main, path = _patched_client(monkeypatch, tmp_path)
    reply = client.post("/api/login", json=LOGIN, headers=HEADERS).json()
    assert reply["saved"] is False and reply["configured"] is False
    assert path.read_text() == "# my accounts\n" and main.CONFIGURED == {}


def test_login_with_remember_saves_and_new_sessions_get_the_account(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    client, main, path = _patched_client(monkeypatch, tmp_path)
    reply = client.post("/api/login", json={**LOGIN, "remember": True}, headers=HEADERS).json()
    assert reply["saved"] is True and reply["configured"] is True
    assert "ACCOUNT_1_PASSWORD=abcdefghijklmnop" in path.read_text()
    fresh = TestClient(main.app).get("/api/accounts").json()
    assert fresh["accounts"] == [{"email": "me@gmail.com", "name": "Me", "configured": True}]
    assert "abcdefghijklmnop" not in str(fresh)


def test_remember_failure_is_reported_but_login_still_succeeds(monkeypatch, tmp_path):
    client, main, path = _patched_client(monkeypatch, tmp_path)
    path.unlink()
    reply = client.post("/api/login", json={**LOGIN, "remember": True}, headers=HEADERS).json()
    assert reply["saved"] is False and "remember_error" in reply and reply["configured"] is False
    assert "abcdefghijklmnop" not in str(reply)
    assert client.get("/api/accounts").json()["accounts"][0]["email"] == "me@gmail.com"  # session still works


def test_forget_removes_the_saved_login_from_file_memory_and_new_sessions(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    client, main, path = _patched_client(monkeypatch, tmp_path)
    client.post("/api/login", json={**LOGIN, "remember": True}, headers=HEADERS)
    reply = client.post("/api/logout", json={"email": "me@gmail.com", "forget": True}, headers=HEADERS).json()
    assert reply["forgotten"] is True and "me@gmail.com" not in path.read_text() and main.CONFIGURED == {}
    assert TestClient(main.app).get("/api/accounts").json()["accounts"] == []


def test_plain_logout_does_not_delete_the_saved_login(monkeypatch, tmp_path):
    client, main, path = _patched_client(monkeypatch, tmp_path)
    client.post("/api/login", json={**LOGIN, "remember": True}, headers=HEADERS)
    reply = client.post("/api/logout", json={"email": "me@gmail.com"}, headers=HEADERS).json()
    assert reply["forgotten"] is False and "me@gmail.com" in path.read_text()


def test_remember_requires_the_csrf_header(monkeypatch, tmp_path):
    client, main, path = _patched_client(monkeypatch, tmp_path)
    assert client.post("/api/login", json={**LOGIN, "remember": True}).status_code == 403
    assert path.read_text() == "# my accounts\n"


def test_signature_delimiter_survives_in_the_plain_text_part():
    message = _build(html_body="<div>Hello</div><div><br></div><div>-- <br>Tester Person<br>QA, Example Co</div>")
    plain = message.get_body(("plain",)).get_content()
    assert "-- \nTester Person\nQA, Example Co" in plain
    html = message.get_body(("html",)).get_content()
    assert "Tester Person" in html and "-- " in html
