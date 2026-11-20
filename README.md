# HackMail Web

A browser version of the [HackMail](https://github.com/DennisFaucher/HackMail) macOS client: warm paper look, folders / message list / reader, Gmail over IMAP and SMTP.
Runs in Docker; nothing about your mail is stored on disk.

```
docker compose up -d --build        # http://127.0.0.1:8080
docker compose logs -f hackmail
docker compose down
```

Sign in with a Google **app password** (Google Account → Security → 2-Step Verification → App passwords), either in the
browser or by listing your accounts in the optional `.accounts` file (below).

## Saved accounts (`.accounts`)
```
cp .accounts.example .accounts && chmod 600 .accounts      # then edit it
docker compose up -d --force-recreate hackmail
```
```
ACCOUNT_1_EMAIL=you@gmail.com
ACCOUNT_1_NAME=Your Full Name
ACCOUNT_1_PASSWORD=abcd efgh ijkl mnop
```
Number further accounts 2, 3, … (spaces in the app password are fine; put comments on their own lines). Saved accounts appear
signed in whenever you open the app. You can also tick **Remember this login** in the sign-in dialog (off by default) to add an
account to this file from the browser, and use **Remove Account…** to sign out and delete its saved entry. Compose mounts the
file read-write at `/data/accounts`, so the passwords are not in the image and not in the container's environment.
If you edit the file with an editor that replaces it (rather than saving in place), run
`docker compose up -d --force-recreate hackmail` so the container sees the new file. The file is git- and docker-ignored. **It is plain text on your disk** (the app only writes to it when you tick Remember or Remove Account): keep it `0600`,
rely on disk encryption, and revoke the app password at Google if the laptop is ever lost. The container runs as uid/gid
1000 so it can read the file; set `HACKMAIL_UID` / `HACKMAIL_GID` if yours differ (`id -u`, `id -g`).

## Signatures
Use **Edit Signature…** in the sidebar to set a signature per account. It is added as `-- ` plus your text at the end of the
editable part of every new message, reply, and forward (above the quoted original), where you can edit or delete it for that
message. Changing the From account swaps it for that account's signature unless you already deleted it. Signatures are plain
text, are not secret, and are kept in this browser's local storage only (not on the server).

## Try it without a real account
```
docker compose --profile dev up -d --build     # http://127.0.0.1:8081, local test mail server
```
Sign in as `tester@example.test` with password `testpass` (or `friend@example.test` / `friendpass`, 16 characters are not required
against the test server).

## Tests
```
docker compose run --rm --no-deps --entrypoint python hackmail -m pytest -q /srv/tests
```

## Security model
- The app listens on **127.0.0.1 only**. Do not publish the port to a network: it has no login of its own.
- App passwords typed into the browser are held in server memory only and forgotten when the container restarts. Passwords
  from `.accounts` are read once at startup into memory; the app never writes, logs, or returns any password. Only account
  addresses and names are remembered by the browser (local storage).
- Anyone who can open the app can use the configured accounts, which is why it listens on loopback only.
- Mail is fetched on demand over TLS and kept only in memory; nothing is cached. Message bodies use `BODY.PEEK`.
- Email HTML is sanitized on the server (no scripts, forms, style sheets, CSS `url()` loads, unsafe links), shown in a
  sandboxed, script-less iframe with its own CSP. Remote images are blocked until you click *Load Images*; links ask first.
- Attachments are listed from MIME metadata and downloaded only when clicked (25 MiB cap, always as a download).
- Bcc recipients are SMTP envelope recipients only; no `Bcc:` header is written. Recipients are capped at 100.
- Mutating requests require a custom header and a same-origin `Origin`; the session cookie is `HttpOnly; SameSite=Strict`.

## Differences from the macOS app
- New mail is detected by polling every 45 seconds (subject-only browser notifications, title badge), not IMAP IDLE.
- No macOS Contacts address suggestions, and no print-to-PDF beyond the browser's own print.
- Plain-text messages show raw URLs; the macOS app's "hide parenthesized tracking URLs" nicety is not ported yet.
