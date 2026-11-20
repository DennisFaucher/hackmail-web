"use strict";
/* HackMail Web client. All text from mail is inserted with textContent; message HTML is shown only in a sandboxed iframe. */

const $ = (id) => document.getElementById(id);
const ICONS = {
  star: '<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',
  reply: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 016 6v3"/>',
  replyall: '<path d="M7 14L2 9l5-5"/><path d="M12 14L7 9l5-5"/><path d="M7 9h8a6 6 0 016 6v3"/>',
  forward: '<path d="M15 14l5-5-5-5"/><path d="M20 9H10a6 6 0 00-6 6v3"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  spam: '<path d="M8 3h8l5 5v8l-5 5H8l-5-5V8z"/><path d="M12 8v5M12 16.5v.5"/>',
  notspam: '<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z"/><path d="M8.5 12l2.5 2.5L16 9.5"/>',
  move: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
  clip: '<path d="M20 11l-8.5 8.5a5 5 0 01-7-7L13 4a3.5 3.5 0 015 5l-8.5 8.5a2 2 0 01-3-3L14 7"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  inbox: '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 3h6l1-3h5"/>',
  sent: '<path d="M22 3L2 11l7 3 3 7z"/><path d="M22 3L9 14"/>',
  draft: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5"/>',
  folder: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>',
  all: '<path d="M3 5h18v4H3zM5 9v10h14V9M10 13h4"/>',
  refresh: '<path d="M20 11a8 8 0 10-2.3 5.7M20 4v7h-7"/>',
};
function icon(name, size) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "i"); s.setAttribute("aria-hidden", "true");
  if (size) { s.style.width = s.style.height = size + "px"; }
  s.innerHTML = ICONS[name] || ICONS.folder;
  return s;
}
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v; else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid);
  return n;
}

/* ------------------------------------------------------------ state */
const state = {
  accounts: [],          // {email, name, signedIn, folders:[]}
  current: null, folder: "INBOX", messages: [], total: 0, selected: null, multi: new Set(), anchor: null,
  sort: { col: "date", dir: -1 }, query: "", busy: null, openToken: 0, listToken: 0, uidNext: {}, unseen: {},
};
const store = {
  load() { try { return JSON.parse(localStorage.getItem("hackmail.accounts") || "[]"); } catch { return []; } },
  save() { try { localStorage.setItem("hackmail.accounts", JSON.stringify(state.accounts.map(({ email, name, signature }) => ({ email, name, signature: signature || "" })))); } catch {} },
};
const acct = () => state.accounts.find((a) => a.email === state.current);
const folderInfo = (kind) => (acct()?.folders || []).find((f) => f[kind]);

/* ------------------------------------------------------------ network */
async function api(url, opts = {}) {
  const res = await fetch(url, { ...opts, headers: { "X-HackMail": "1", ...(opts.headers || {}) }, credentials: "same-origin" });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch {}
    throw new Error(typeof detail === "string" ? detail : "Request failed.");
  }
  return res;
}
/** Stream NDJSON progress; resolves with the final result, rejects with the server's error text. */
async function stream(url, opts = {}, onProgress = () => {}, signal) {
  const res = await api(url, { ...opts, signal });
  const reader = res.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let result;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let i;
    while ((i = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, i); buffer = buffer.slice(i + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.error) throw new Error(msg.error);
      if (msg.done) result = msg.result; else onProgress(msg);
    }
  }
  if (result === undefined) throw new Error("The connection ended before the operation finished.");
  return result;
}
const qs = (o) => new URLSearchParams(o).toString();

/* ------------------------------------------------------------ busy pill + toast */
let pillTimer = null;
function showPill(text, step, total) {
  const pill = $("pill"); pill.hidden = false;
  const started = state.busy?.started || Date.now();
  const label = step ? `Step ${step} of ${total} · ${text}` : text;
  pill.replaceChildren(el("span", { class: "spin" }), el("span", { text: label }), el("span", { class: "elapsed" }));
  const tick = () => { const e = pill.querySelector(".elapsed"); if (e) e.textContent = `${Math.floor((Date.now() - started) / 1000)} s`; };
  clearInterval(pillTimer); pillTimer = setInterval(tick, 1000); tick();
}
function hidePill() { clearInterval(pillTimer); $("pill").hidden = true; }
let toastTimer = null;
function toast(text, isError = false) {
  const t = $("toast"); t.textContent = text; t.className = isError ? "err" : ""; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), isError ? 8000 : 5000);
}
/** Run one exclusive remote operation with a progress pill; Esc cancels it. */
async function busy(label, fn) {
  if (state.busy) return;
  const controller = new AbortController();
  state.busy = { started: Date.now(), controller }; showPill(label); renderTools();
  try { return await fn((p) => showPill(p.text, p.step, p.total), controller.signal); }
  catch (e) { if (e.name !== "AbortError") toast(e.message, true); }
  finally { state.busy = null; hidePill(); renderTools(); renderList(); }
}

/* ------------------------------------------------------------ accounts + folders */
async function init() {
  state.accounts = store.load().map((a) => ({ ...a, signedIn: false, folders: [] }));
  try {
    const { accounts } = await (await api("/api/accounts")).json();
    for (const s of accounts) {
      const known = state.accounts.find((a) => a.email === s.email);
      if (known) Object.assign(known, { signedIn: true, configured: !!s.configured, name: s.name || known.name });
      else state.accounts.push({ email: s.email, name: s.name, signedIn: true, configured: !!s.configured, folders: [] });
    }
  } catch {}
  store.save(); renderSidebar();
  const first = state.accounts.find((a) => a.signedIn) || state.accounts[0];
  if (first) await selectAccount(first.email); else openSignIn();
  setInterval(pollInbox, 45000);
}
async function selectAccount(email) {
  const a = state.accounts.find((x) => x.email === email);
  if (!a) return;
  if (!a.signedIn) { openSignIn(email); return; }
  state.current = email; state.folder = "INBOX"; state.messages = []; state.selected = null; state.multi.clear(); state.query = "";
  $("search").value = ""; renderReader(null); renderSidebar(); renderList();
  await busy("Loading your folder list…", async (progress, signal) => {
    const r = await stream("/api/folders?" + qs({ account: email }), {}, progress, signal);
    a.folders = r.folders; renderSidebar();
    await loadList(progress, signal);
  });
  pollInbox(true);
}
async function loadList(progress = () => {}, signal) {
  const token = ++state.listToken;
  const r = await stream("/api/messages?" + qs({ account: state.current, folder: state.folder, q: state.query }), {}, progress, signal);
  if (token !== state.listToken) return;
  const keep = state.selected;
  state.messages = r.messages; state.total = r.total;
  state.multi = new Set([...state.multi].filter((u) => r.messages.some((m) => m.uid === u)));
  if (!r.messages.some((m) => m.uid === keep)) { state.selected = null; renderReader(null); }
  renderList();
}
function openFolder(path) {
  if (state.busy) return;
  state.folder = path; state.query = ""; $("search").value = ""; state.selected = null; state.multi.clear(); state.messages = [];
  renderReader(null); renderSidebar(); renderList();
  busy("Opening the folder…", (progress, signal) => loadList(progress, signal));
}
function renderSidebar() {
  const fl = $("folders"); fl.replaceChildren();
  const a = acct();
  const order = (f) => (f.inbox ? 0 : f.flagged ? 1 : f.sent ? 2 : f.drafts ? 3 : f.all ? 4 : f.spam ? 5 : f.trash ? 6 : 7);
  for (const f of [...(a?.folders || [])].sort((x, y) => order(x) - order(y) || x.name.localeCompare(y.name))) {
    const name = f.inbox ? "inbox" : f.sent ? "sent" : f.drafts ? "draft" : f.trash ? "trash" : f.spam ? "spam" : f.all ? "all" : "folder";
    const count = f.inbox && state.unseen[state.current] ? el("span", { class: "count", text: String(state.unseen[state.current]) }) : null;
    fl.append(el("button", { class: f.path === state.folder ? "active" : "", onclick: () => openFolder(f.path), title: f.path }, icon(name), el("span", { text: f.name }), count));
  }
  const ac = $("accounts"); ac.replaceChildren();
  for (const x of state.accounts) {
    ac.append(el("button", { class: x.email === state.current ? "active" : "", onclick: () => selectAccount(x.email), title: !x.signedIn ? "Add this account's app password" : x.configured ? "Connected; login remembered on this computer" : "Connected for this session only" },
      el("span", { class: "dot" + (x.signedIn ? " on" : "") }), el("span", { text: x.email, style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap" }),
      x.configured ? el("span", { class: "count", text: "saved", title: "Login remembered in the .accounts file" }) : null));
  }
  const total = Object.values(state.unseen).reduce((s, n) => s + n, 0);
  document.title = (total ? `(${total}) ` : "") + "HackMail";
}

/* ------------------------------------------------------------ sign in */
function openSignIn(email = "") {
  const known = state.accounts.find((a) => a.email === email);
  $("siEmail").value = email; $("siName").value = known?.name || ""; $("siPassword").value = ""; $("siError").textContent = "";
  $("siRemember").checked = false; $("siRememberHint").hidden = true;
  $("signin").showModal(); (email ? $("siPassword") : $("siEmail")).focus();
}
$("siCancel").onclick = () => $("signin").close();
$("siRemember").onchange = (e) => { $("siRememberHint").hidden = !e.target.checked; };
$("signinForm").addEventListener("submit", (e) => e.preventDefault());
$("siSubmit").onclick = async () => {
  const email = $("siEmail").value.trim(), name = $("siName").value.trim(), password = $("siPassword").value;
  if (!email || !password) { $("siError").textContent = "Enter the email address and app password."; return; }
  $("siSubmit").disabled = true; $("siError").textContent = "Signing in…";
  try {
    if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    const r = await (await api("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, name, password, remember: $("siRemember").checked }) })).json();
    let a = state.accounts.find((x) => x.email === r.email);
    if (!a) { a = { email: r.email, name, signedIn: true, folders: r.folders }; state.accounts.push(a); }
    Object.assign(a, { name, signedIn: true, folders: r.folders, configured: !!r.configured });
    $("siPassword").value = ""; store.save(); $("signin").close(); await selectAccount(r.email);
    if (r.saved) toast("Login remembered on this computer."); else if (r.remember_error) toast("Signed in, but the login was not saved: " + r.remember_error, true);
  } catch (err) { $("siError").textContent = err.message; }
  finally { $("siSubmit").disabled = false; }
};
$("addAccountBtn").onclick = () => openSignIn();
$("removeAccountBtn").onclick = async () => {
  const a = acct(); if (!a || state.busy) return;
  const message = a.configured
    ? `Sign out of ${a.email} and delete its saved login from the .accounts file?`
    : `Remove ${a.email}? Its app password is forgotten and you will need to enter it again to use the account.`;
  if (!confirm(message)) return;
  try {
    const r = await (await api("/api/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: a.email, forget: !!a.configured }) })).json();
    if (r.forget_error) { toast("The saved login could not be removed: " + r.forget_error, true); return; }
    state.accounts = state.accounts.filter((x) => x.email !== a.email); store.save();
    state.current = null; state.messages = []; state.selected = null; state.multi.clear(); renderReader(null); renderList(); renderTools();
    toast(r.forgotten ? "Account removed and its saved login deleted." : "Account removed.");
    const next = state.accounts.find((x) => x.signedIn);
    if (next) await selectAccount(next.email); else { renderSidebar(); openSignIn(); }
  } catch (e) { toast(e.message, true); }
};

/* ------------------------------------------------------------ list */
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  if (isNaN(d)) return "";
  const sameDay = d.toDateString() === now.toDateString();
  return sameDay ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : d.toLocaleDateString([], { month: "short", day: "numeric" }) + (d.getFullYear() === now.getFullYear() ? "" : `, ${d.getFullYear()}`);
}
function sortedMessages() {
  const { col, dir } = state.sort;
  const key = { date: (m) => m.date, sender: (m) => m.sender.toLowerCase(), subject: (m) => m.subject.toLowerCase(), flagged: (m) => (m.flagged ? 1 : 0) }[col];
  return [...state.messages].sort((a, b) => (key(a) < key(b) ? -dir : key(a) > key(b) ? dir : b.uid - a.uid));
}
function renderList() {
  const list = $("list"); const msgs = sortedMessages(); list.replaceChildren();
  if (!msgs.length) {
    if (!state.busy) list.append(el("div", { class: "empty" }, el("div", { text: state.query ? "No messages matched the Gmail search." : "No messages" })));
    return;
  }
  const arrow = (c) => (state.sort.col === c ? (state.sort.dir > 0 ? " ▲" : " ▼") : "");
  const h = (c, label, extra = "") => el("div", { class: "h " + extra, text: label + arrow(c), onclick: () => { state.sort = { col: c, dir: state.sort.col === c ? -state.sort.dir : c === "date" ? -1 : 1 }; renderList(); } });
  list.append(el("div", { class: "head" }, h("flagged", "★"), el("div", { "aria-label": "Attachment" }, icon("clip", 12)), h("subject", "Subject"), h("sender", "From"), h("date", "Date", "right")));
  for (const m of msgs) {
    const selected = state.selected === m.uid || state.multi.has(m.uid);
    const star = el("button", { class: "star" + (m.flagged ? " on" : ""), title: m.flagged ? "Remove flag" : "Flag", "aria-label": "Flag",
      onclick: (e) => { e.stopPropagation(); doAction("star" , [m.uid], m.flagged ? "unstar" : "star"); } }, icon("star"));
    list.append(el("div", { class: "row" + (m.unread ? " unread" : "") + (selected ? " sel" : ""), "data-uid": m.uid, onclick: (e) => selectMessage(m.uid, e) },
      star, el("div", {}, m.has_attachment ? icon("clip", 13) : null), el("div", { class: "subject", text: m.subject, title: m.subject }),
      el("div", { class: "sender", text: m.sender, title: m.sender_email }), el("div", { class: "date", text: fmtDate(m.date) })));
  }
}
function selectMessage(uid, e) {
  if (state.busy) return;
  const ordered = sortedMessages().map((m) => m.uid);
  if (e.metaKey || e.ctrlKey) {
    if (state.selected !== null && !state.multi.size) state.multi.add(state.selected);
    state.multi.has(uid) ? state.multi.delete(uid) : state.multi.add(uid); state.anchor ??= uid; state.selected = state.multi.size === 1 ? [...state.multi][0] : null;
  } else if (e.shiftKey && (state.anchor ?? state.selected) !== null) {
    const a = ordered.indexOf(state.anchor ?? state.selected), b = ordered.indexOf(uid);
    state.multi = new Set(ordered.slice(Math.min(a, b), Math.max(a, b) + 1)); state.selected = null;
  } else { state.multi.clear(); state.anchor = uid; state.selected = uid; }
  renderList(); renderTools(); $("list").focus();
  if (state.multi.size > 1) { renderReader(null, `${state.multi.size} messages selected`); return; }
  if (state.selected !== null) openMessage(uid);
}
$("list").addEventListener("keydown", (e) => {
  if (state.busy || !["ArrowUp", "ArrowDown"].includes(e.key)) return;
  e.preventDefault();
  const ordered = sortedMessages().map((m) => m.uid); if (!ordered.length) return;
  const i = state.selected === null ? (e.key === "ArrowDown" ? -1 : ordered.length) : ordered.indexOf(state.selected);
  const next = ordered[Math.min(Math.max(i + (e.key === "ArrowDown" ? 1 : -1), 0), ordered.length - 1)];
  selectMessage(next, {});
  $("list").querySelector(`[data-uid="${next}"]`)?.scrollIntoView({ block: "nearest" });
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && state.busy) { state.busy.controller.abort(); toast("Cancelled."); } });

/* ------------------------------------------------------------ reader */
async function openMessage(uid, images = false) {
  const token = ++state.openToken;
  await busy("Connecting securely to Gmail…", async (progress, signal) => {
    const m = await stream("/api/message?" + qs({ account: state.current, folder: state.folder, uid, images: images ? 1 : 0 }), {}, progress, signal);
    if (token !== state.openToken || state.selected !== uid) return;
    const row = state.messages.find((x) => x.uid === uid); if (row && m.was_unread) { row.unread = false; pollInbox(true); }
    renderReader(m);
  });
}
function addrText(list) { return list.map((a) => (a.name && a.name !== a.email ? `${a.name} <${a.email}>` : a.email)).join(", "); }
function renderReader(m, emptyText = "No message selected") {
  const r = $("reader"); r.replaceChildren();
  if (!m) { r.append(el("div", { class: "empty", text: emptyText })); return; }
  const from = m.from[0]; const initial = (from.name || from.email || "?").trim()[0]?.toUpperCase() || "?";
  const when = new Date(m.date);
  const head = el("div", { class: "rhead" },
    el("div", { class: "meta", text: "MESSAGE" + (isNaN(when) ? "" : " · " + when.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })) }),
    el("h1", { text: m.subject }),
    el("div", { class: "who" }, el("div", { class: "avatar", text: initial }), el("div", {}, el("div", { class: "name", text: from.name || from.email }), el("div", { class: "addr", text: from.email }))),
    m.to.length ? el("div", { class: "rcpt", text: "To: " + addrText(m.to) }) : null,
    m.cc.length ? el("div", { class: "rcpt", text: "Cc: " + addrText(m.cc) }) : null);
  r.append(head);
  if (m.blocked_images > 0 && !m.images_allowed) {
    r.append(el("div", { class: "banner" }, el("span", { text: `${m.blocked_images} remote image${m.blocked_images === 1 ? " was" : "s were"} blocked to protect your privacy.` }),
      el("button", { text: "Load Images for This Message", onclick: () => confirm("Loading remote images tells the sender you opened this message. Load them?") && openMessage(m.uid, true) })));
  }
  if (m.partial) r.append(el("div", { class: "banner", text: "This message is very large; only the first part is shown." }));
  if (m.attachments.length) {
    r.append(el("div", { class: "atts" }, m.attachments.map((a) => a.too_large
      ? el("button", { disabled: true, title: "Larger than the 25 MiB limit" }, icon("clip", 13), `${a.filename} (too large)`)
      : el("a", { class: "btn", href: "/api/attachment?" + qs({ account: state.current, folder: state.folder, uid: m.uid, part: a.part }), download: a.filename },
        el("button", { type: "button", title: "Download " + a.filename }, icon("clip", 13), `${a.filename} · ${fmtSize(a.size)}`)))));
  }
  const frame = el("iframe", { id: "frame", sandbox: "allow-same-origin", title: "Message body", src: "/render/" + m.render });
  frame.addEventListener("load", () => {
    try { frame.contentDocument.addEventListener("click", (e) => { const a = e.target.closest("a[href]"); if (a) { e.preventDefault(); confirmLink(a.href); } }); } catch {}
  });
  r.append(frame);
  r.dataset.uid = m.uid; r.currentMessage = m;
  renderTools();
}
const fmtSize = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB");
function confirmLink(href) {
  $("linkTarget").textContent = href; const dlg = $("linkDlg"); dlg.showModal();
  $("linkOpen").onclick = (e) => { e.preventDefault(); dlg.close(); if (/^(https?:|mailto:|tel:)/i.test(href)) window.open(href, "_blank", "noopener,noreferrer"); };
  $("linkCopy").onclick = () => { navigator.clipboard?.writeText(href); toast("Link copied."); };
}

/* ------------------------------------------------------------ toolbar + actions */
function selectedUids() { return state.multi.size > 1 ? [...state.multi] : state.selected !== null ? [state.selected] : []; }
function tool(name, title, fn, disabled) { return el("button", { title, "aria-label": title, onclick: fn, disabled: disabled || false }, icon(name)); }
function renderTools() {
  const t = $("tools"); t.replaceChildren(); const uids = selectedUids(); const none = !uids.length || !!state.busy || !state.current;
  const inSpam = !!folderInfo("spam") && (acct().folders.find((f) => f.path === state.folder) || {}).spam;
  const inTrash = (acct()?.folders.find((f) => f.path === state.folder) || {}).trash;
  if (state.multi.size > 1) t.append(el("span", { class: "sel", text: `${state.multi.size} selected` }));
  t.append(tool("move", "Move to…", (e) => moveMenu(e.currentTarget), none || (acct()?.folders.length || 0) < 2));
  t.append(tool(inSpam ? "notspam" : "spam", inSpam ? "Not Spam: move to Inbox" : "Mark as Spam", () => doAction(inSpam ? "notspam" : "spam", uids, inSpam ? "notspam" : "spam"), none || !folderInfo(inSpam ? "inbox" : "spam")));
  t.append(tool("trash", inTrash ? "Delete permanently" : "Move to Trash", () => doDelete(uids, inTrash), none || (!inTrash && !folderInfo("trash"))));
  if (state.multi.size > 1) { t.append(tool("close", "Clear multi-selection", () => { state.multi.clear(); state.selected = null; renderList(); renderTools(); renderReader(null); })); return; }
  t.append(el("span", { class: "sep" }));
  const one = state.selected !== null && !state.busy; const loaded = $("reader").currentMessage && $("reader").dataset.uid == state.selected;
  t.append(tool("reply", "Reply", () => beginCompose("reply"), !(one && loaded)));
  t.append(tool("replyall", "Reply All", () => beginCompose("replyall"), !(one && loaded)));
  t.append(tool("forward", "Forward", () => beginCompose("forward"), !(one && loaded)));
  t.append(el("span", { class: "sep" }), tool("refresh", "Refresh", () => busy("Refreshing…", (p, s) => loadList(p, s)), !!state.busy || !state.current));
}
function moveMenu(button) {
  document.querySelector(".menu")?.remove();
  const rect = button.getBoundingClientRect();
  const menu = el("div", { class: "menu", style: `position:fixed;left:${rect.left}px;top:${rect.bottom + 4}px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:4px;max-height:50vh;overflow:auto;z-index:30;box-shadow:0 4px 16px #0003` });
  for (const f of acct().folders.filter((f) => f.path !== state.folder)) {
    menu.append(el("button", { style: "display:block;width:100%;text-align:left;border:0;background:none;padding:5px 12px", text: f.name, onclick: () => { menu.remove(); doAction("move", selectedUids(), "move", f.path); } }));
  }
  document.body.append(menu);
  setTimeout(() => document.addEventListener("click", () => menu.remove(), { once: true }), 0);
}
async function doDelete(uids, permanent) {
  if (permanent && !confirm(`Permanently delete ${uids.length === 1 ? "this message" : "these messages"}? This cannot be undone.`)) return;
  doAction(permanent ? "delete" : "trash", uids, permanent ? "delete" : "trash");
}
const DONE_TEXT = { star: "Flag updated.", unstar: "Flag updated.", move: "moved", trash: "moved to Trash", delete: "permanently deleted", spam: "marked as spam", notspam: "moved to Inbox (not spam)" };
async function doAction(_kind, uids, action, destination) {
  if (!uids.length) return;
  const folders = { trash: folderInfo("trash")?.path, spam: folderInfo("spam")?.path, inbox: (acct().folders.find((f) => f.inbox) || {}).path };
  await busy("Connecting securely to Gmail…", async (progress, signal) => {
    const r = await stream("/api/action", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account: state.current, folder: state.folder, uids, action, destination, folders }) }, progress, signal);
    if (r.removed) {
      state.messages = state.messages.filter((m) => !uids.includes(m.uid));
      if (uids.includes(state.selected)) { state.selected = null; renderReader(null); }
      state.multi.clear();
      toast(`${r.count === 1 ? "Message" : r.count + " messages"} ${DONE_TEXT[action]}.`);
    } else {
      for (const m of state.messages) if (uids.includes(m.uid)) m.flagged = action === "star" ? true : action === "unstar" ? false : m.flagged;
    }
  });
}
$("searchForm").addEventListener("submit", (e) => {
  e.preventDefault(); if (state.busy || !state.current) return;
  state.query = $("search").value.trim(); state.selected = null; state.multi.clear(); renderReader(null);
  busy(state.query ? "Gmail is searching this folder…" : "Reloading the folder…", (p, s) => loadList(p, s));
});

/* ------------------------------------------------------------ compose */
const cm = { mode: "", attachments: [], quote: null, images: 0, busy: false };
function setCompose({ title, from, to = "", cc = "", bcc = "", subject = "", quoteHtml = "", mode = "", uid = 0 }) {
  $("composeTitle").textContent = title; cm.mode = mode; cm.uid = uid; cm.folder = state.folder; cm.attachments = []; cm.images = $("reader").currentMessage?.images_allowed ? 1 : 0;
  const sel = $("cmFrom"); sel.replaceChildren(...state.accounts.filter((a) => a.signedIn).map((a) => el("option", { value: a.email, text: a.name ? `${a.name} <${a.email}>` : a.email })));
  sel.value = from; $("cmTo").value = to; $("cmCc").value = cc; $("cmBcc").value = bcc; $("cmSubject").value = subject;
  const showCc = !!(cc || bcc); $("ccRow").hidden = $("bccRow").hidden = !showCc; $("ccbccToggle").hidden = showCc;
  $("cmBody").replaceChildren(el("div", {}, el("br")), el("div", {}, el("br"))); applySignature(from, true); renderCompAttachments(); $("cmStatus").textContent = ""; $("cmStatus").className = "";
  const q = $("cmQuote"); q.hidden = !quoteHtml;
  if (quoteHtml) { $("cmQuoteFrame").srcdoc = "<!doctype html><meta charset=utf-8><style>body{font:14px system-ui;color:#2b2217}</style>" + quoteHtml; q.querySelector(".quote-label").textContent = mode === "forward" ? "Forwarded message (read-only)" : "Original message (read-only)"; }
  $("cmSend").disabled = false; $("composeDlg").showModal();
  if (to) focusBodyStart(); else $("cmTo").focus();
}
/* Signature: "-- " then the text, as a plain editable block (same shape as the macOS app's draft). */
const SIG_LIMIT = 1000;
function signatureNode(text) {
  const t = (text || "").trim(); if (!t) return null;
  const div = el("div", { "data-hm-sig": "" }); div.append("-- ", el("br"));
  const lines = t.split(/\r?\n/);
  lines.forEach((line, i) => { div.append(line); if (i < lines.length - 1) div.append(el("br")); });
  return div;
}
/** Insert the account's signature (`insert`), or swap an existing block for another account's; never re-add a deleted one. */
function applySignature(email, insert = false) {
  const body = $("cmBody"), existing = body.querySelector("[data-hm-sig]");
  if (!insert && !existing) return;   // the user deleted it on purpose: do not bring it back when From changes
  const node = signatureNode(state.accounts.find((a) => a.email === email)?.signature);
  if (existing) { node ? existing.replaceWith(node) : existing.remove(); } else if (node) body.append(node);
}
function focusBodyStart() {
  const body = $("cmBody"); body.focus();
  const range = document.createRange(); range.setStart(body.firstChild || body, 0); range.collapse(true);
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
}
$("cmFrom").onchange = () => applySignature($("cmFrom").value);
$("editSigBtn").onclick = () => {
  if (!state.accounts.length) { openSignIn(); return; }
  const select = $("sigAccount"); select.replaceChildren(...state.accounts.map((a) => el("option", { value: a.email, text: a.email })));
  select.value = state.current || state.accounts[0].email; loadSignatureText(); $("sigDlg").showModal(); $("sigText").focus();
};
function loadSignatureText() { $("sigText").value = state.accounts.find((a) => a.email === $("sigAccount").value)?.signature || ""; }
$("sigAccount").onchange = loadSignatureText;
$("sigCancel").onclick = () => $("sigDlg").close();
$("sigSave").onclick = () => {
  const a = state.accounts.find((x) => x.email === $("sigAccount").value); if (!a) return;
  a.signature = $("sigText").value.replace(/\r\n?/g, "\n").trim().slice(0, SIG_LIMIT); store.save(); $("sigDlg").close();
  toast(a.signature ? "Signature saved." : "Signature removed.");
};
function newMessage() { if (state.current) setCompose({ title: "New Message", from: state.current }); }
$("composeBtn").onclick = newMessage;
async function beginCompose(mode) {
  const uid = state.selected; if (uid === null) return;
  await busy("Preparing the quoted message…", async (progress, signal) => {
    const ctx = await stream("/api/compose-context?" + qs({ account: state.current, folder: state.folder, uid, mode, images: $("reader").currentMessage?.images_allowed ? 1 : 0 }), {}, progress, signal);
    setCompose({ title: ctx.title, from: state.current, to: ctx.to.join(", "), cc: ctx.cc.join(", "), subject: ctx.subject, quoteHtml: ctx.quote_html, mode, uid });
  });
}
$("showCc").onclick = () => { $("ccRow").hidden = $("bccRow").hidden = false; $("ccbccToggle").hidden = true; $("cmCc").focus(); };
$("cmCancel").onclick = () => { if (!cm.busy) $("composeDlg").close(); };
$("composeDlg").addEventListener("cancel", (e) => { if (cm.busy) e.preventDefault(); });
$("fmt").addEventListener("click", (e) => { const b = e.target.closest("button[data-cmd]"); if (b) { document.execCommand(b.dataset.cmd); $("cmBody").focus(); } });
$("cmLink").onclick = () => {
  const url = prompt("Link address (http, https, or mailto):", "https://"); if (!url) return;
  if (!/^(https?:|mailto:)/i.test(url)) { toast("Only http, https, and mailto links are allowed.", true); return; }
  document.execCommand("createLink", false, url);
};
$("cmAttach").onclick = () => $("cmFiles").click();
const MAX_ATTACH = 25 * 1024 * 1024;
$("cmFiles").onchange = (e) => {
  for (const f of e.target.files) {
    if (cm.attachments.reduce((s, a) => s + a.size, 0) + f.size > MAX_ATTACH) { toast("Attachments are limited to 25 MiB in total.", true); break; }
    cm.attachments.push(f);
  }
  e.target.value = ""; renderCompAttachments();
};
function renderCompAttachments() {
  $("cmAttachments").replaceChildren(...cm.attachments.map((f, i) => el("li", {}, icon("clip", 13), el("span", { text: `${f.name} · ${fmtSize(f.size)}` }),
    el("button", { type: "button", class: "linklike", text: "Remove", onclick: () => { cm.attachments.splice(i, 1); renderCompAttachments(); } }))));
}
$("cmBody").addEventListener("paste", (e) => {
  const file = [...(e.clipboardData?.files || [])].find((f) => /^image\/(png|jpeg|gif|webp)$/.test(f.type)); if (!file) return;
  e.preventDefault();
  const reader = new FileReader(); reader.onload = () => document.execCommand("insertHTML", false, `<img src="${reader.result}" alt="Pasted image">`); reader.readAsDataURL(file);
});
$("cmSend").onclick = async () => {
  if (cm.busy) return; cm.busy = true; $("cmSend").disabled = true; const status = $("cmStatus"); status.className = "";
  const started = Date.now(); let step = "Preparing message…";
  const tick = setInterval(() => (status.replaceChildren(el("span", { class: "spin" }), el("span", { text: `${step} · ${Math.floor((Date.now() - started) / 1000)} s` }))), 500);
  try {
    const fd = new FormData();
    fd.set("account", $("cmFrom").value); fd.set("to", $("cmTo").value); fd.set("cc", $("cmCc").value); fd.set("bcc", $("cmBcc").value);
    fd.set("subject", $("cmSubject").value); fd.set("html", $("cmBody").innerHTML);
    if (cm.mode) { fd.set("quote_mode", cm.mode); fd.set("quote_folder", cm.folder); fd.set("quote_uid", cm.uid); fd.set("quote_images", cm.images); }
    for (const f of cm.attachments) fd.append("files", f, f.name);
    const r = await stream("/api/send", { method: "POST", body: fd }, (p) => { step = `Step ${p.step} of ${p.total} · ${p.text}`; });
    clearInterval(tick); $("composeDlg").close(); toast(`Message sent to ${r.recipients} recipient${r.recipients === 1 ? "" : "s"}.`);
  } catch (e) { clearInterval(tick); status.className = "err"; status.textContent = e.message; $("cmSend").disabled = false; }
  finally { cm.busy = false; }
};

/* ------------------------------------------------------------ new-mail polling (subject-only notifications) */
let polling = false;
async function pollInbox(quiet = false) {
  if (polling || !state.current || !acct()?.signedIn) return; polling = true;
  const email = state.current;
  try {
    const since = state.uidNext[email] || 0;
    const s = await (await api("/api/inbox-state?" + qs({ account: email, since_uid: since }))).json();
    state.unseen[email] = s.unseen;
    if (since && s.uidnext > since && !quiet) {
      if (s.subjects?.length && "Notification" in window && Notification.permission === "granted") {
        const extra = Math.max(0, s.uidnext - since - s.subjects.length);
        new Notification(s.subjects.length === 1 && !extra ? "New message" : `${s.uidnext - since} new messages`, { body: s.subjects.join("\n") + (extra ? `\n…and ${extra} more` : ""), silent: false });
      }
      if (state.folder === "INBOX" && !state.busy && !state.query) busy("Refreshing…", (p, sig) => loadList(p, sig));
    }
    state.uidNext[email] = s.uidnext; renderSidebar();
  } catch {} finally { polling = false; }
}

/* ------------------------------------------------------------ resizable list/reader split */
(() => {
  const pane = $("listPane"), split = $("splitter"); let y0, h0;
  const move = (e) => { pane.style.height = Math.max(120, Math.min(window.innerHeight - 300, h0 + e.clientY - y0)) + "px"; };
  split.addEventListener("pointerdown", (e) => { y0 = e.clientY; h0 = pane.offsetHeight; split.setPointerCapture(e.pointerId); split.addEventListener("pointermove", move); });
  split.addEventListener("pointerup", () => split.removeEventListener("pointermove", move));
})();

renderTools();
init();
