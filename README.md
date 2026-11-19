# Graph Inbox to Maildir

One-way copy of a Microsoft 365 Inbox into a local Maildir. Himalaya v2.2.1
with `+msgraph` and `+maildir` handles listing, fetching, and saving. Ortie
supplies tokens. The Python 3.11+ wrapper handles cron locking and an indexed
SQLite ledger. It does not call Graph directly, deliver over LMTP, or run Sieve.

## Set up

1. As the local mailbox owner, initialize Ortie with your delegated Graph
   permissions (`Mail.Read`, `offline_access`, and optionally `User.Read`).
   Verify Ortie can refresh tokens without a desktop session or prompt.
2. Create and subscribe to `Work` in Roundcube. Determine its actual Dovecot
   Maildir path. For a common Maildir++ configuration it is
   `/home/alice/Maildir/.Work`; the path must contain `cur`, `new`, and `tmp`.
3. Add two accounts to your existing Himalaya configuration:

   ```toml
   [accounts.o365]
   msgraph.user-id = "me"
   msgraph.auth.token.command = ["/usr/bin/ortie", "-c", "/home/alice/.config/ortie/config.toml", "token", "show", "-a", "work"]

   [accounts.local_backup]
   maildir.root = "/home/alice/Maildir/.Work"
   ```

   Here `work` is the Ortie account name. The local account is rooted inside
   the destination mailbox: the wrapper saves to `.` via
   `maildir message save`, which places new messages in that mailbox's `new`
   directory. Use the real path, not the IMAP name.
4. Install `graph_maildir.py` and copy `config.example.toml` to a private
   `config.toml`. Adjust the paths and account names. Run manually as the
   mailbox owner before enabling cron:

   ```sh
   /usr/bin/python3 /home/alice/.local/bin/graph_maildir.py \
     --config /home/alice/.config/graph-maildir/config.toml run
   ```

   **The first run copies the entire current Inbox, including read messages.**
   Confirm the messages appear in Roundcube. Then add to the user's crontab:

   ```cron
   HOME=/home/alice
   PATH=/usr/bin:/bin
   * * * * * /usr/bin/python3 /home/alice/.local/bin/graph_maildir.py --config /home/alice/.config/graph-maildir/config.toml run
   ```

Logs go to syslog (`mail` facility, `graph-maildir` ident); warnings and errors
also go to stderr. Child command output is withheld from logs. If a command
fails, run it manually under the same Unix account to diagnose it; redirect
raw message output to a private file rather than displaying it.

`index_command` is optional. Dovecot normally notices Maildir changes when the
folder is opened. Enable `doveadm index` only if you want proactive indexing
and your cron user can run it without elevated privileges. The command runs
after each poll, including partial imports, so failed indexing is retried
without copying mail again. Direct saves bypass delivery-time Sieve and
Dovecot's delivery-time quota enforcement.

## Bookkeeping and cleanup

- The wrapper relists the Inbox each time. It uses Himalaya's released
  `--skip` option with the server-supplied offset. If Graph returns a
  continuation that is not expressible as an offset, it reports an error
  rather than silently treating that page as the end. Changes to the Inbox
  during offset pagination can shift results; a later full pass can pick up
  messages still in Inbox.
- `maildir.sqlite3` maps ordinary Graph IDs to local save status. Local
  moves, copies, and deletions do not affect the remote mailbox or this
  ledger. A remote move that changes the Graph ID can cause a second import.
  Keep the database backed up and keep its account names and underlying
  mailbox configuration stable. Use a fresh state directory for a different
  source or destination. The older LMTP importer's ledger is not reused.
- The wrapper downloads each message to a private temporary file before
  attempting to save it. This uses Himalaya's file input rather than stdin
  so invalid UTF-8 is reported instead of risking a truncated save in
  v2.2.1. Such a failure needs inspection before manual retry.
- The importer never marks or deletes mail remotely. Clean up in Outlook
  only after confirming the mail is saved locally. Mail removed before
  discovery or download may be missed.

Before calling Himalaya's save command, the wrapper commits a `saving` row.
It marks the row `saved` only on successful exit. If a save fails, times out
or is interrupted, the row is held rather than automatically retried. Use
`status` instead of `run` to list held messages. Each local copy gets an
`X-Local-Graph-Import-ID` header containing the marker from that status report.
An authorized administrator can search for it, including in folders to which
the message might have been moved:

```sh
doveadm search -u alice HEADER X-Local-Graph-Import-ID MARKER
```

Use `resolve MARKER --as saved` if you've confirmed the save. Use
`resolve MARKER --as retry` to let a later poll try it again if the remote
message is still in Inbox. An absent marker does not prove a save never
happened: the local copy might already have been deleted. Automatic retries
would risk duplicates.

Run the local tests with `python3 -m unittest -v`; they do not contact
Microsoft 365 or require access to a real mailbox.
