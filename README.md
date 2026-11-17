# Graph Inbox to Dovecot LMTP

A single Python 3.11+ standard-library script, intended for cron on Gentoo/OpenRC.
It copies the signed-in Microsoft 365 user's Inbox to one fixed local recipient.
No external Python packages, daemon, MySQL, or Maildir access are needed.

Ortie supplies access tokens. Graph is called directly rather than through
Himalaya so delta checkpoints and immutable message IDs do not depend on a
particular Himalaya release or JSON schema. Himalaya can remain installed but
is not used by this script.

## Before starting

- Confirm workplace policy permits these local copies.
- Initialize Ortie as the same Unix user that will run cron. Use your own client
  ID and delegated Graph `Mail.Read`, `User.Read`, and `offline_access` scopes.
  Configure automatic token refresh. Do not copy upstream example write/send
  permissions or its example client ID.
- Confirm the configured token command prints only the access token and works
  without a browser, unlocked desktop keyring, or password prompt in a cron-like
  environment. Tokens are never stored in this importer's SQLite database.
  Interactive reauthentication can still be required by tenant policy.
- Give that Unix user access to the Dovecot LMTP socket. Do not make the socket
  world-writable; use appropriate ownership/group permissions. Socket access
  may authorize delivery to other local users too.
- Choose the same recipient identity already accepted by your Dovecot LMTP
  configuration. This is unrelated to the original message's To/Cc headers.
- Review existing Sieve rules. They still apply, including discard and redirect.
  A successful delivery means Dovecot accepted responsibility, not necessarily
  that a copy remains in Inbox.

## Install

Replace `alice`, paths, and recipient below with your actual values.
As the mailbox owner:

```sh
install -d -m 700 /home/alice/.local/lib/graph-lmtp
install -d -m 700 /home/alice/.config/graph-lmtp
install -d -m 700 /home/alice/.local/state/graph-lmtp
install -m 700 graph_lmtp.py /home/alice/.local/lib/graph-lmtp/graph_lmtp.py
install -m 600 config.example.toml /home/alice/.config/graph-lmtp/config.toml
```

Edit the configuration. Paths must be absolute. Adjust the Ortie command to your
installed version if necessary; it is an argument array, not a shell command.
The script uses the system HTTPS trust store and refuses HTTP redirects.
It targets the public Microsoft Graph cloud, not sovereign-cloud endpoints.

**The first run imports all messages currently in Inbox, including read mail.**
There is no date cutoff or mark-as-read side effect. Existing local mail is not
matched or deduplicated against an initially empty ledger.

Run manually first:

```sh
/usr/bin/python3 /home/alice/.local/lib/graph-lmtp/graph_lmtp.py \
  --config /home/alice/.config/graph-lmtp/config.toml run
```

Check Roundcube and syslog before scheduling it. Add to that user's crontab:

```cron
HOME=/home/alice
PATH=/usr/bin:/bin
* * * * * /usr/bin/python3 /home/alice/.local/lib/graph-lmtp/graph_lmtp.py --config /home/alice/.config/graph-lmtp/config.toml run
```

The script logs with ident `graph-lmtp`, facility `mail`. Warnings/errors also
go to stderr, allowing cron mail if configured. Ensure syslog-ng routes this
facility somewhere you monitor. No message bodies, access tokens, or server
response text are logged. Successful deliveries log an opaque marker.

## Bookkeeping and failure policy

The private state directory contains `ledger.sqlite3` and `import.lock`.
SQLite's primary-key index provides on-disk ID lookups; a partial index selects
pending messages. Historical IDs are not loaded into memory. One invocation
holds an exclusive advisory lock; overlapping cron runs exit without doing work.

The first successful `/me` request binds the ledger to that Graph user ID and
configured local recipient. A mismatch stops the importer. Use a distinct
directory for another source/destination pair.

Graph delta pagination records newly discovered IDs and the next checkpoint in
one SQLite transaction. Read/unread changes and repeated discovery do not
schedule another copy. Remote removal events never delete local mail or erase
delivery history. HTTP 410 clears only the expired discovery checkpoint;
the next run safely rescans using the retained ledger.

Messages are retrieved through `/me/messages/{immutable-id}/$value` as full MIME,
without setting the remote read flag. A message discovered in Inbox can still
be fetched if later moved within the same source mailbox. Messages moved out
before discovery, deleted before download, or moved to an archive mailbox may
be missed or become unavailable. Inbox-only polling cannot guarantee capturing
every transient arrival.

Each message follows these states:

| State | Meaning |
| --- | --- |
| `pending` | Discovered, eligible for download and delivery |
| `sending` | Committed immediately before LMTP DATA; outcome may be uncertain |
| `delivered` | Final LMTP acceptance recorded, or explicitly resolved by operator |
| `failed` | Permanent LMTP rejection, remote HTTP 404, or malformed MIME; requires review |

Known LMTP 4xx rejections are retried on later processing. Permanent 5xx
rejections are held. Network failure before DATA leaves the entry pending.
Network/process failure after committing `sending` leaves it held, **never
automatically retried**. This conservative policy can require operator action
even if DATA was never transmitted, but avoids guessing after ambiguous delivery.
A failure to commit SQLite success also leaves a held entry.

One connection and one envelope recipient are used per delivery, so the final
LMTP DATA response belongs to that recipient. No SMTP queue is involved.
The envelope sender is null (`MAIL FROM:<>`) to suppress automatic replies and
bounces for copied messages. Ordinary headers, MIME parts, and attachments are
retained, with transport line endings normalized to CRLF. A single
`X-Local-Graph-Import-ID` header is added, replacing any source instances of that
specific header. Original arrival dates and remote read flags are not imported.

Only one MIME message is held in memory at a time; there is no durable spool.
Allow extra memory for DATA quoting and line normalization. Messages exceeding
`max_message_bytes` stop processing with an error and remain pending for an
operator to address. The remote message must remain retrievable until delivery.

Graph 429/503 Retry-After deadlines are persisted, including HTTP-date values.
Runs defer Graph access until that deadline (a missing deadline uses 60 seconds).
Other connection/Graph failures stop the run and are logged. A 401 invokes
the token helper once more; a second failure requires attention.

## Held entries and reconciliation

Show state counts and held IDs/markers without contacting Graph:

```sh
python3 graph_lmtp.py --config /absolute/path/config.toml status
```

For an uncertain entry, an administrator can search the user's local mailboxes:

```sh
doveadm search -u alice HEADER X-Local-Graph-Import-ID MARKER_FROM_STATUS
```

Search across all relevant mailboxes, since Sieve or the user may have moved
the copy. Verify the result before resolving the corresponding remote ID:

```sh
python3 graph_lmtp.py --config /absolute/path/config.toml \
  resolve 'REMOTE_ID_FROM_STATUS' --as delivered
```

If you explicitly decide to attempt delivery again:

```sh
python3 graph_lmtp.py --config /absolute/path/config.toml \
  resolve 'REMOTE_ID_FROM_STATUS' --as pending
```

**An absent marker does not prove nondelivery.** Sieve may have discarded or
redirected the message, or someone may have deleted it. Retrying an uncertain
entry can duplicate mail. A Graph 404 must be addressed remotely before retry.
Resolve commands only accept held (`sending`/`failed`) entries.

To force fresh discovery without losing deduplication history:

```sh
python3 graph_lmtp.py --config /absolute/path/config.toml rescan
```

Never delete delivered rows to save space: that allows old messages to be
imported again. Back up the entire state directory while the importer is stopped
or use SQLite's online backup API. Restoring an older ledger can replay messages
delivered since that backup. Keeping Roundcube mail alone is not a ledger backup.

Exit codes: 0 for success, an overlapping scheduled run, or an active retry
window; 1 for an error or held entries after a completed run; 2 for CLI misuse.

## Local checks

```sh
python3 -m unittest -v
```

Tests use temporary directories and fake/local protocol peers, never real Graph
accounts, Ortie credentials, or an installed Dovecot instance.
