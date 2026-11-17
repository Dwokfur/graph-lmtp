#!/usr/bin/env python3
"""Copy a delegated Microsoft Graph Inbox to one local Dovecot LMTP recipient."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import fcntl
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import subprocess
import sys
import syslog
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


GRAPH = "https://graph.microsoft.com/v1.0"
INITIAL_CURSOR = GRAPH + "/me/mailFolders/inbox/messages/delta?$select=id"
MARKER_HEADER = b"X-Local-Graph-Import-ID"


class ImportFailure(Exception):
    pass


class GraphFailure(ImportFailure):
    def __init__(self, status, retry_at=0):
        super().__init__(f"Graph HTTP {status}")
        self.status = status
        self.retry_at = retry_at


@dataclass(frozen=True)
class Config:
    state_dir: Path
    token_command: list[str]
    lmtp_socket: str
    recipient: str
    timeout: int = 120
    max_message_bytes: int = 100 * 1024 * 1024

    @classmethod
    def load(cls, path):
        with open(path, "rb") as stream:
            data = tomllib.load(stream)
        required = {"state_dir", "token_command", "lmtp_socket", "recipient"}
        allowed = required | {"timeout", "max_message_bytes"}
        if not required <= data.keys() or data.keys() - allowed:
            raise ImportFailure("Configuration has missing or unknown keys")
        for key in ("state_dir", "lmtp_socket", "recipient"):
            if not isinstance(data[key], str) or not data[key]:
                raise ImportFailure(f"{key} must be a nonempty string")
        command = data["token_command"]
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or not arg for arg in command)
            or not Path(command[0]).is_absolute()
        ):
            raise ImportFailure("token_command must be an argv array with an absolute executable")
        for key in ("timeout", "max_message_bytes"):
            if key in data and (type(data[key]) is not int or data[key] <= 0):
                raise ImportFailure(f"{key} must be a positive integer")
        if not Path(data["state_dir"]).is_absolute() or not Path(data["lmtp_socket"]).is_absolute():
            raise ImportFailure("state_dir and lmtp_socket must be absolute paths")
        # Accept a fixed ASCII local recipient, never a header or an address list.
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~@-]+", data["recipient"]):
            raise ImportFailure("recipient must be a single ASCII envelope address")
        return cls(**{**data, "state_dir": Path(data["state_dir"])})


def log(message, priority=syslog.LOG_INFO):
    syslog.syslog(priority, message)
    if priority <= syslog.LOG_WARNING:
        print(message, file=sys.stderr)


@contextmanager
def locked_store(directory):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.geteuid():
        raise ImportFailure("State directory must be owned by this user and not be a symlink")
    if directory.stat().st_mode & 0o077:
        raise ImportFailure("State directory must have mode 0700")
    with open(directory / "import.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        db = sqlite3.connect(directory / "ledger.sqlite3")
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    remote_id TEXT PRIMARY KEY COLLATE BINARY,
                    marker TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL
                        CHECK (state IN ('pending', 'sending', 'delivered', 'failed')),
                    updated_at INTEGER NOT NULL,
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS pending_messages
                    ON messages(state) WHERE state = 'pending';
            """)
            yield db
        finally:
            db.close()


def metadata(db, key):
    row = db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def put_metadata(db, key, value):
    db.execute(
        "INSERT INTO metadata VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def change_state(db, remote_id, state, error=None):
    with db:
        db.execute(
            "UPDATE messages SET state=?, updated_at=?, last_error=? WHERE remote_id=?",
            (state, int(time.time()), error, remote_id),
        )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    if not isinstance(url, str):
        raise ImportFailure("Graph continuation URL is missing or invalid")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != "graph.microsoft.com"
        or not parts.path.startswith("/v1.0/")
        or parts.fragment
        or any(ord(char) <= 32 or ord(char) >= 127 for char in url)
    ):
        raise ImportFailure("Refusing unexpected Graph URL")
    return url


def retry_time(value):
    if not value:
        return 0
    try:
        return time.time() + max(0, int(value))
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0


class Graph:
    def __init__(self, config):
        self.config = config
        self.opener = build_opener(NoRedirect())
        self.token = None

    def access_token(self):
        result = subprocess.run(
            self.config.token_command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.config.timeout,
            check=False,
        )
        if result.returncode:
            raise ImportFailure("Token helper failed; run it interactively to diagnose (output withheld)")
        token = result.stdout.strip()
        if not re.fullmatch(rb"[\x21-\x7e]+", token):
            raise ImportFailure("Token helper did not return a single raw access token")
        return token.decode("ascii")

    def get(self, url, mime=False):
        validate_url(url)
        for attempt in range(2):
            if self.token is None:
                self.token = self.access_token()
            request = Request(url, headers={
                "Authorization": "Bearer " + self.token,
                "Prefer": 'IdType="ImmutableId", odata.maxpagesize=100',
                "Accept": "*/*" if mime else "application/json",
            })
            try:
                with self.opener.open(request, timeout=self.config.timeout) as response:
                    limit = self.config.max_message_bytes if mime else 8 * 1024 * 1024
                    content = response.read(limit + 1)
                    if len(content) > limit:
                        raise ImportFailure("Graph response exceeds configured size limit")
                if mime:
                    return content
                try:
                    result = json.loads(content)
                except (ValueError, UnicodeError) as exc:
                    raise ImportFailure("Invalid Graph JSON response") from exc
                if not isinstance(result, dict):
                    raise ImportFailure("Expected a Graph JSON object")
                return result
            except HTTPError as exc:
                status = exc.code
                retry_at = retry_time(exc.headers.get("Retry-After"))
                exc.close()
                if status == 401 and attempt == 0:
                    self.token = None
                    continue
                if status in (429, 503) and not retry_at:
                    retry_at = time.time() + 60
                raise GraphFailure(status, retry_at) from None
            except (URLError, OSError, HTTPException) as exc:
                raise ImportFailure(f"Graph connection failed ({type(exc).__name__})") from None


def bind_identity(db, user_id, recipient):
    if not isinstance(user_id, str) or not user_id:
        raise ImportFailure("Graph /me did not return a user ID")
    with db:
        for key, value in (("source_user_id", user_id), ("recipient", recipient)):
            previous = metadata(db, key)
            if previous is not None and previous != value:
                raise ImportFailure(f"Ledger {key} mismatch; refusing to mix accounts or destinations")
            put_metadata(db, key, value)


def save_page(db, page):
    values = page.get("value")
    if not isinstance(values, list):
        raise ImportFailure("Graph delta page has no message list")
    next_link = page.get("@odata.nextLink")
    delta_link = page.get("@odata.deltaLink")
    if bool(next_link) == bool(delta_link):
        raise ImportFailure("Expected exactly one Graph continuation or delta link")
    cursor = validate_url(next_link or delta_link)
    with db:
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ImportFailure("Graph delta entry has no valid message ID")
            if "@removed" in item:
                continue
            db.execute(
                """INSERT INTO messages VALUES (?, ?, 'pending', ?, NULL)
                   ON CONFLICT(remote_id) DO NOTHING""",
                (item["id"], uuid.uuid4().hex, int(time.time())),
            )
        put_metadata(db, "cursor", cursor)
    return bool(next_link)


def prepare_message(raw, marker):
    # SMTP DATA requires CRLF. Keep MIME structure and encoded attachments intact.
    raw = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    headers, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ImportFailure("Downloaded MIME has no header/body separator")
    kept = []
    skipping = False
    for line in headers.split(b"\r\n"):
        if not line.startswith((b" ", b"\t")):
            skipping = line.partition(b":")[0].lower() == MARKER_HEADER.lower()
        if not skipping:
            kept.append(line)
    header = MARKER_HEADER + b": " + marker.encode("ascii")
    return b"\r\n".join([header, *kept]) + separator + body


def rejection_state(code):
    return "failed" if 500 <= code < 600 else "pending"


def deliver(db, row, message, config, factory=smtplib.LMTP):
    client = factory(timeout=config.timeout, local_hostname="localhost")
    try:
        code, _ = client.connect(config.lmtp_socket)
        if code != 220:
            raise ImportFailure(f"LMTP greeting rejected ({code})")
        code, _ = client.ehlo()
        if code != 250:
            raise ImportFailure(f"LMTP LHLO rejected ({code})")
        options = []
        if any(byte >= 128 for byte in message.partition(b"\r\n\r\n")[0]):
            if not client.has_extn("smtputf8"):
                raise ImportFailure("Message has UTF-8 headers but LMTP lacks SMTPUTF8")
            options.append("SMTPUTF8")
        if any(byte >= 128 for byte in message):
            if not client.has_extn("8bitmime"):
                raise ImportFailure("Message has 8-bit content but LMTP lacks 8BITMIME")
            options.append("BODY=8BITMIME")
        # A null reverse path prevents automatic replies/bounces for copied mail.
        code, _ = client.mail("", options=options)
        if code != 250:
            change_state(db, row["remote_id"], rejection_state(code), f"LMTP sender rejected ({code})")
            log(f"Sender rejected for marker={row['marker']} code={code}", syslog.LOG_ERR)
            return False
        code, _ = client.rcpt(config.recipient)
        if code not in (250, 251):
            change_state(db, row["remote_id"], rejection_state(code), f"LMTP envelope rejected ({code})")
            log(f"Envelope rejected for marker={row['marker']} code={code}", syslog.LOG_ERR)
            return False
        # This durable boundary precedes DATA. Any unclassified failure after it is ambiguous.
        change_state(db, row["remote_id"], "sending")
        try:
            code, _ = client.data(message)
        except smtplib.SMTPDataError as exc:
            code = exc.smtp_code
            if not 400 <= code < 600:
                change_state(db, row["remote_id"], "sending", f"Unexpected LMTP DATA invitation ({code})")
                log(f"Unexpected DATA invitation marker={row['marker']} code={code}", syslog.LOG_ERR)
                return False
        if code == 250:
            change_state(db, row["remote_id"], "delivered")
            log(f"Delivered marker={row['marker']}")
            return True
        if 400 <= code < 600:
            change_state(db, row["remote_id"], rejection_state(code), f"LMTP DATA rejected ({code})")
        else:
            change_state(db, row["remote_id"], "sending", f"Unexpected LMTP DATA response ({code})")
        log(f"DATA not accepted for marker={row['marker']} code={code}", syslog.LOG_ERR)
        return False
    finally:
        # QUIT failure must not change an already confirmed delivery outcome.
        client.close()


def deliver_pending(db, graph, config, after_rowid=0):
    delivered = 0
    failures = 0
    rows = db.execute(
        "SELECT rowid, * FROM messages WHERE state='pending' AND rowid > ? ORDER BY rowid",
        (after_rowid,),
    ).fetchall()
    for row in rows:
        after_rowid = row["rowid"]
        try:
            raw = graph.get(GRAPH + "/me/messages/" + quote(row["remote_id"], safe="") + "/$value", mime=True)
        except GraphFailure as exc:
            if exc.status == 404:
                change_state(db, row["remote_id"], "failed", "Remote message unavailable (HTTP 404)")
                log(f"Remote message unavailable marker={row['marker']}", syslog.LOG_ERR)
                failures += 1
                continue
            raise
        try:
            message = prepare_message(raw, row["marker"])
        except ImportFailure as exc:
            change_state(db, row["remote_id"], "failed", str(exc))
            log(f"Invalid MIME marker={row['marker']}", syslog.LOG_ERR)
            failures += 1
            continue
        if deliver(db, row, message, config):
            delivered += 1
        else:
            failures += 1
    return delivered, failures, after_rowid


def run(db, graph, config):
    retry_at = float(metadata(db, "retry_at") or 0)
    if time.time() < retry_at:
        log("Graph retry window active; deferring this run")
        return 0
    user = graph.get(GRAPH + "/me?$select=id")
    bind_identity(db, user.get("id"), config.recipient)
    delivered, failures, after_rowid = deliver_pending(db, graph, config)
    while True:
        cursor = metadata(db, "cursor") or INITIAL_CURSOR
        try:
            page = graph.get(cursor)
        except GraphFailure as exc:
            if exc.status == 410:
                with db:
                    db.execute("DELETE FROM metadata WHERE key='cursor'")
                raise ImportFailure("Graph checkpoint expired; next run will rescan without clearing the ledger") from None
            raise
        more = save_page(db, page)
        count, errors, after_rowid = deliver_pending(db, graph, config, after_rowid)
        delivered += count
        failures += errors
        if not more:
            break
    held = db.execute("SELECT COUNT(*) FROM messages WHERE state IN ('sending', 'failed')").fetchone()[0]
    log(f"Run complete: delivered={delivered} failed_attempts={failures} held={held}",
        syslog.LOG_WARNING if held or failures else syslog.LOG_INFO)
    return 1 if held or failures else 0


def status(db):
    counts = dict(db.execute("SELECT state, COUNT(*) FROM messages GROUP BY state"))
    held = [dict(row) for row in db.execute(
        "SELECT remote_id, marker, state, last_error FROM messages WHERE state IN ('sending', 'failed')"
    )]
    print(json.dumps({"counts": counts, "held": held}, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="discover Inbox messages and deliver pending copies")
    commands.add_parser("status", help="show counts and entries requiring attention")
    commands.add_parser("rescan", help="reset discovery checkpoint, retaining all delivery history")
    resolve = commands.add_parser("resolve", help="manually resolve a held message")
    resolve.add_argument("remote_id")
    resolve.add_argument("--as", dest="new_state", choices=["pending", "delivered"], required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    syslog.openlog("graph-lmtp", syslog.LOG_PID, syslog.LOG_MAIL)
    db = None
    try:
        config = Config.load(args.config)
        with locked_store(config.state_dir) as db:
            if db is None:
                if args.command == "run":
                    log("Another importer invocation holds the lock")
                    return 0
                raise ImportFailure("Importer is busy; try again later")
            try:
                if args.command == "status":
                    status(db)
                    return 0
                if args.command == "rescan":
                    with db:
                        db.execute("DELETE FROM metadata WHERE key='cursor'")
                    log("Discovery checkpoint reset; delivery history retained", syslog.LOG_WARNING)
                    return 0
                if args.command == "resolve":
                    row = db.execute("SELECT state FROM messages WHERE remote_id=?", (args.remote_id,)).fetchone()
                    if row is None or row["state"] not in ("sending", "failed"):
                        raise ImportFailure("Only an existing sending/failed entry can be resolved")
                    change_state(db, args.remote_id, args.new_state)
                    log(f"Operator resolved held entry as {args.new_state}", syslog.LOG_WARNING)
                    return 0
                return run(db, Graph(config), config)
            except GraphFailure as exc:
                if exc.retry_at:
                    with db:
                        put_metadata(db, "retry_at", exc.retry_at)
                raise
    except (ImportFailure, OSError, sqlite3.Error, smtplib.SMTPException,
            subprocess.SubprocessError, ValueError) as exc:
        # Never include arbitrary server responses, helper output, or Graph URLs in logs.
        detail = str(exc) if isinstance(exc, ImportFailure) else type(exc).__name__
        log(f"Importer stopped: {detail}; run status to inspect held deliveries", syslog.LOG_ERR)
        return 1


if __name__ == "__main__":
    sys.exit(main())
