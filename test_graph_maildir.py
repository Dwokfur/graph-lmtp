import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import graph_maildir as app


class ImportTests(unittest.TestCase):
    def setUp(self):
        state = self.enterContext(tempfile.TemporaryDirectory())
        self.settings = Path(state) / "config.toml"
        self.settings.write_text(f"""
state_dir = {json.dumps(state)}
himalaya = "/usr/bin/himalaya"
himalaya_config = "/home/alice/himalaya.toml"
source_account = "o365"
local_account = "local_backup"
index_command = ["/usr/bin/doveadm", "index", "-u", "alice", "Work"]
""")
        self.config = app.Config.load(self.settings)
        self.calls = []
        self.saved = []
        self.failures = set()
        self.continuation = "https://graph.microsoft.com/v1.0/me/messages?%24skip=7"
        self.enterContext(patch.object(app, "log"))
        self.enterContext(patch.object(app, "command", side_effect=self.fake_command))

    def fake_command(self, argv, timeout, action, stdout=None):
        self.calls.append(argv)
        if action in self.failures:
            raise app.ImportFailure(f"{action} failed")
        if action == "Himalaya Inbox listing":
            result = {"messages": [{"id": "item-1"}]}
            if argv[-1] == "0":
                result["next_page"] = self.continuation
            else:
                self.assertEqual(argv[-1], "7")
                result["messages"].append({"id": "item-2"})
            return json.dumps(result).encode()
        if action == "Himalaya MIME download":
            os.write(stdout.fileno(), f"Subject: {argv[-1]}\r\n\r\nbody\r\n".encode())
        if action == "Himalaya Maildir save":
            self.assertEqual(argv[-7:-1], ["maildir", "message", "save", "--maildir", ".", "--"])
            self.saved.append(Path(argv[-1]).read_bytes())

    def cli(self, *args):
        with patch.object(app.os, "umask"), patch.object(app.syslog, "openlog"):
            return app.main(["--config", str(self.settings), *args])

    def test_duplicates_and_index_failures_do_not_replay_saves(self):
        self.failures.add("Dovecot indexing")
        self.assertEqual(self.cli("run"), 1)
        self.assertEqual(len(self.saved), 2)
        self.assertIn(b"\r\nSubject: item-1\r\n\r\nbody\r\n", self.saved[0])
        self.failures.clear()
        self.assertEqual(self.cli("run"), 0)
        self.assertEqual(len(self.saved), 2)
        self.assertEqual(sum(call[0] == "/usr/bin/doveadm" for call in self.calls), 2)
        self.assertEqual(list(Path(self.config.state_dir).glob("*.eml")), [])

    def test_uncertain_saves_are_held_until_manually_resolved(self):
        self.failures.add("Himalaya Maildir save")
        self.assertEqual(self.cli("run"), 1)
        self.failures.clear()
        self.assertEqual(self.cli("run"), 1)
        self.assertEqual(self.saved, [])
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(self.cli("status"), 0)
        held = json.loads(output.getvalue())["held"]
        self.assertEqual(len(held), 2)
        self.assertEqual(self.cli("resolve", held[0]["marker"], "--as", "retry"), 0)
        self.assertEqual(self.cli("resolve", held[1]["marker"], "--as", "saved"), 0)
        self.assertEqual(self.cli("run"), 0)
        self.assertEqual(len(self.saved), 1)

    def test_download_failure_can_be_retried_without_operator_action(self):
        self.failures.add("Himalaya MIME download")
        self.assertEqual(self.cli("run"), 1)
        self.assertEqual(self.saved, [])
        self.failures.clear()
        self.assertEqual(self.cli("run"), 0)
        self.assertEqual(len(self.saved), 2)

    def test_unsupported_continuation_does_not_report_completion(self):
        self.continuation = "https://graph.microsoft.com/v1.0/me/messages?%24skiptoken=opaque"
        self.assertEqual(self.cli("run"), 1)
        self.assertEqual(len(self.saved), 1)

    def test_overlapping_run_does_not_invoke_himalaya(self):
        with app.locked_store(self.config):
            self.assertEqual(self.cli("run"), 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
