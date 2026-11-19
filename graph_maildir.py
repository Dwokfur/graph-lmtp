#!/usr/bin/env python3
"""Copy a Microsoft 365 Inbox into a local Maildir using Himalaya."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import syslog
import tempfile
import tomllib
from urllib.parse import parse_qs, urlsplit
import uuid


class ImportFailure(Exception):
    pass


@dataclass(frozen=True)
class Config:
    state_dir: str
    himalaya: str
    himalaya_config: str
    source_account: str
    local_account: str
    index_command: tuple[str, ...] = ()
    timeout: int = 120

    @classmethod
    def load(cls, path):
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        paths = ("state_dir", "himalaya", "himalaya_config")
        accounts = ("source_account", "local_account")
        required = set(paths + accounts)
        if not required <= data.keys() or data.keys() - (required | {"index_command", "timeout"}):
            raise ImportFailure("Configuration has missing or unknown keys")
        for key in paths + accounts:
            if not isinstance(data[key], str) or not data[key].strip() or "\0" in data[key]:
                raise ImportFailure(f"{key} must be a nonempty string")
        for key in paths:
            if not Path(data[key]).is_absolute():
                raise ImportFailure(f"{key} must be an absolute path")
        if data["source_account"] == data["local_account"]:
            raise ImportFailure("Use separate Himalaya accounts for Graph and Maildir")
        timeout = data.get("timeout", 120)
        if type(timeout) is not int or timeout <= 0:
            raise ImportFailure("timeout must be a positive integer")
        index = data.get("index_command", [])
        if not isinstance(index, list) or any(
            not isinstance(arg, str) or not arg or "\0" in arg for arg in index
        ) or (index and not Path(index[0]).is_absolute()):
            raise ImportFailure("index_command must be an argv array with an absolute executable")
        return cls(**{**data, "index_command": tuple(index)})

    def command(self, account, *args):
        return [self.himalaya, "-c", self.himalaya_config, "-a", account, *args]


def log(message, priority=syslog.LOG_INFO):
    syslog.syslog(priority, message)
    if priority <= syslog.LOG_WARNING:
        print(message, file=sys.stderr)


@contextmanager
def locked_store(config):
    directory = Path(config.state_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
        raise ImportFailure("State directory must be private, owned by this user, and not a symlink")
    with (directory / "import.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        # A separate database avoids mixing ordinary Graph IDs with the old immutable IDs.
        db = sqlite3.connect(directory / "maildir.sqlite3")
        try:
            db.row_factory = sqlite3.Row
            db.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS configuration (identity TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                    remote_id TEXT NOT NULL PRIMARY KEY,
                    marker TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (state IN ('saving', 'saved'))
                );
                CREATE INDEX IF NOT EXISTS held ON messages(state) WHERE state = 'saving';
            """)
            identity = json.dumps([config.himalaya_config, config.source_account, config.local_account])
            with db:
                row = db.execute("SELECT identity FROM configuration").fetchone()
                if row is None:
                    db.execute("INSERT INTO configuration VALUES (?)", (identity,))
                elif row[0] != identity:
                    raise ImportFailure("Ledger belongs to a different configuration or account pair")
            yield db
        finally:
            db.close()


def command(argv, timeout, action, stdout=subprocess.DEVNULL):
    try:
        result = subprocess.run(
            argv, timeout=timeout, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImportFailure(f"{action} failed ({type(exc).__name__})") from None
    if result.returncode:
        raise ImportFailure(f"{action} failed (exit {result.returncode})")
    return result.stdout


def next_offset(link, previous):
    # v2.2.1 prints a next link but only accepts --skip, not the full link.
    # Use the server's offset, which can differ from the number of items returned.
    if isinstance(link, str):
        query = parse_qs(urlsplit(link).query, keep_blank_values=True)
        values = query.get("$skip", [])
        if "$skiptoken" not in query and len(values) == 1:
            value = values[0]
            if value.isascii() and value.isdecimal() and len(value) <= 10:
                offset = int(value)
                if previous < offset <= 2**32 - 1:
                    return offset
    raise ImportFailure("Inbox continuation cannot be followed with Himalaya --skip")


def inbox_ids(config):
    offset = 0
    while True:
        argv = config.command(config.source_account, "--json", "msgraph", "messages", "list",
                              "--folder", "inbox", "--select", "id", "--top", "100",
                              "--skip", str(offset))
        output = command(argv, config.timeout, "Himalaya Inbox listing", subprocess.PIPE)
        try:
            page = json.loads(output)
        except (ValueError, UnicodeError):
            raise ImportFailure("Himalaya Inbox listing did not return valid JSON") from None
        if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
            raise ImportFailure("Himalaya Inbox listing did not return a messages array")
        for item in page["messages"]:
            remote_id = item.get("id") if isinstance(item, dict) else None
            if not isinstance(remote_id, str) or not remote_id or "\0" in remote_id:
                raise ImportFailure("Himalaya Inbox listing returned an invalid message ID")
            yield remote_id
        if page.get("next_page") is None:
            return
        offset = next_offset(page["next_page"], offset)


def save_message(db, config, remote_id):
    marker = uuid.uuid4().hex
    with tempfile.NamedTemporaryFile(dir=config.state_dir, suffix=".eml") as message:
        message.write(f"X-Local-Graph-Import-ID: {marker}\r\n".encode("ascii"))
        message.flush()
        header_size = message.tell()
        argv = config.command(config.source_account, "msgraph", "messages", "get", "--raw", "--", remote_id)
        command(argv, config.timeout, "Himalaya MIME download", message)
        if os.fstat(message.fileno()).st_size <= header_size:
            raise ImportFailure("Himalaya returned an empty message")
        with db:
            db.execute("INSERT INTO messages VALUES (?, ?, 'saving')", (remote_id, marker))
        argv = config.command(config.local_account, "maildir", "message", "save",
                              "--maildir", ".", "--", message.name)
        try:
            command(argv, config.timeout, "Himalaya Maildir save")
        except ImportFailure as exc:
            raise ImportFailure(f"{exc}; held marker={marker}") from None
        with db:
            db.execute("UPDATE messages SET state='saved' WHERE remote_id=?", (remote_id,))
        log(f"Saved marker={marker}")


def run(db, config):
    saved = failed = 0
    try:
        for remote_id in inbox_ids(config):
            if db.execute("SELECT 1 FROM messages WHERE remote_id=?", (remote_id,)).fetchone():
                continue
            try:
                save_message(db, config, remote_id)
            except ImportFailure as exc:
                log(str(exc), syslog.LOG_ERR)
                failed += 1
            else:
                saved += 1
    finally:
        if config.index_command:
            try:
                command(config.index_command, config.timeout, "Dovecot indexing")
            except ImportFailure as exc:
                log(str(exc), syslog.LOG_ERR)
                failed += 1
    held = db.execute("SELECT COUNT(*) FROM messages WHERE state='saving'").fetchone()[0]
    log(f"Run complete: saved={saved} errors={failed} held={held}",
        syslog.LOG_WARNING if failed or held else syslog.LOG_INFO)
    return int(bool(failed or held))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("run", help="import previously unseen Inbox messages")
    commands.add_parser("status", help="show saved count and uncertain saves")
    resolve = commands.add_parser("resolve", help="resolve one uncertain save")
    resolve.add_argument("marker")
    resolve.add_argument("--as", dest="outcome", choices=("saved", "retry"), required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    syslog.openlog("graph-maildir", syslog.LOG_PID, syslog.LOG_MAIL)
    try:
        config = Config.load(args.config)
        with locked_store(config) as db:
            if db is None:
                if args.action == "run":
                    return 0
                raise ImportFailure("Importer is busy; try again later")
            if args.action == "run":
                return run(db, config)
            if args.action == "status":
                counts = dict(db.execute("SELECT state, COUNT(*) FROM messages GROUP BY state"))
                held = [dict(row) for row in db.execute(
                    "SELECT remote_id, marker FROM messages WHERE state='saving'"
                )]
                print(json.dumps({"saved": counts.get("saved", 0), "held": held}, indent=2))
                return 0
            with db:
                sql = ("UPDATE messages SET state='saved' WHERE state='saving' AND marker=?"
                       if args.outcome == "saved" else "DELETE FROM messages WHERE state='saving' AND marker=?")
                if db.execute(sql, (args.marker,)).rowcount != 1:
                    raise ImportFailure("No held save with that marker")
            return 0
    except (ImportFailure, OSError, sqlite3.Error, ValueError) as exc:
        detail = str(exc) if isinstance(exc, ImportFailure) else type(exc).__name__
        log(f"Importer stopped: {detail}", syslog.LOG_ERR)
        return 1


if __name__ == "__main__":
    sys.exit(main())
