import io
import json
from pathlib import Path
import smtplib
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import graph_lmtp as app


MIME = (
    b"From: sender@example.org\r\n"
    b"To: original@example.org\r\n"
    b"Subject: Test\r\n"
    b"Message-ID: <original@example.org>\r\n"
    b"\r\nHello\r\n.dot\r\n"
)
DELTA = app.INITIAL_CURSOR + "&$deltatoken=next"


def page(*items, more=False):
    return {
        "value": [{"id": item} if isinstance(item, str) else item for item in items],
        "@odata.nextLink" if more else "@odata.deltaLink": DELTA,
    }


class FakeLMTP:
    reply = 250
    rcpt_reply = 250
    data_error = None
    messages = []
    on_data = None

    def __init__(self, **kwargs):
        pass

    def connect(self, path):
        return 220, b"ready"

    def ehlo(self):
        return 250, b"ready"

    def has_extn(self, name):
        return True

    def mail(self, sender, options):
        if sender != "":
            raise AssertionError("Expected null envelope sender")
        return 250, b"sender"

    def rcpt(self, recipient):
        return self.rcpt_reply, b"recipient"

    def data(self, message):
        if type(self).on_data:
            type(self).on_data()
        self.messages.append(message)
        if self.data_error:
            raise self.data_error
        return self.reply, b"result"

    def close(self):
        pass


class ImporterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = patch.object(app, "log").start()
        self.addCleanup(patch.stopall)
        self.directory = Path(self.temp.name)
        self.config = app.Config(self.directory / "state", ["/usr/bin/ortie"], "/not/a/socket", "alice")
        self.store = app.locked_store(self.config.state_dir)
        self.db = self.store.__enter__()
        self.addCleanup(self.store.__exit__, None, None, None)
        FakeLMTP.reply = 250
        FakeLMTP.rcpt_reply = 250
        FakeLMTP.data_error = None
        FakeLMTP.messages = []
        FakeLMTP.on_data = None

    def row(self, remote_id="one"):
        return self.db.execute("SELECT * FROM messages WHERE remote_id=?", (remote_id,)).fetchone()

    def deliver(self, remote_id="one"):
        row = self.row(remote_id)
        return app.deliver(self.db, row, app.prepare_message(MIME, row["marker"]), self.config, FakeLMTP)

    def test_repeat_discovery_preserves_delivery_and_case_sensitive_ids(self):
        app.save_page(self.db, page("one", "ONE"))
        self.assertTrue(self.deliver())
        marker = self.row()["marker"]
        app.save_page(self.db, page("one", {"id": "one", "@removed": {"reason": "deleted"}}))
        self.assertEqual(self.row()["state"], "delivered")
        self.assertEqual(self.row()["marker"], marker)
        self.assertEqual(self.row("ONE")["state"], "pending")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    def test_bad_page_rolls_back_ids_and_checkpoint_together(self):
        app.save_page(self.db, page("original"))
        bad = page("new", {"invalid": "entry"})
        bad["@odata.deltaLink"] += "different"
        with self.assertRaises(app.ImportFailure):
            app.save_page(self.db, bad)
        self.assertIsNone(self.row("new"))
        self.assertEqual(app.metadata(self.db, "cursor"), DELTA)

    def test_identity_change_cannot_mix_accounts_or_destinations(self):
        app.bind_identity(self.db, "source-one", "alice")
        for user, recipient in (("source-two", "alice"), ("source-one", "bob")):
            with self.assertRaises(app.ImportFailure):
                app.bind_identity(self.db, user, recipient)
        self.assertEqual(app.metadata(self.db, "source_user_id"), "source-one")
        self.assertEqual(app.metadata(self.db, "recipient"), "alice")

    def test_state_is_committed_before_data_and_not_retried_after_disconnect(self):
        app.save_page(self.db, page("one"))

        def verify_durable_state():
            import sqlite3
            with sqlite3.connect(self.config.state_dir / "ledger.sqlite3") as other:
                self.assertEqual(other.execute("SELECT state FROM messages").fetchone()[0], "sending")

        FakeLMTP.on_data = verify_durable_state
        FakeLMTP.data_error = smtplib.SMTPServerDisconnected("response lost")
        with self.assertRaises(smtplib.SMTPServerDisconnected):
            self.deliver()
        self.assertEqual(self.row()["state"], "sending")
        graph = Mock()
        self.assertEqual(app.deliver_pending(self.db, graph, self.config), (0, 0, 0))
        graph.get.assert_not_called()

    def test_response_codes_choose_safe_states(self):
        for code, expected in ((250, "delivered"), (451, "pending"), (550, "failed"), (354, "sending")):
            with self.subTest(code=code):
                remote_id = str(code)
                app.save_page(self.db, page(remote_id))
                FakeLMTP.reply = code
                self.assertEqual(self.deliver(remote_id), code == 250)
                self.assertEqual(self.row(remote_id)["state"], expected)

    def test_rejected_data_invitation_is_not_a_success(self):
        app.save_page(self.db, page("one"))
        FakeLMTP.data_error = smtplib.SMTPDataError(250, b"unexpected")
        self.assertFalse(self.deliver())
        self.assertEqual(self.row()["state"], "sending")

    def test_recipient_rejection_never_sends_content(self):
        app.save_page(self.db, page("one"))
        FakeLMTP.rcpt_reply = 550
        self.assertFalse(self.deliver())
        self.assertEqual(self.row()["state"], "failed")
        self.assertEqual(FakeLMTP.messages, [])

    def test_marker_replaces_source_header_without_reserializing_mime(self):
        raw = (
            b"From: a@example.org\nx-local-graph-import-id: forged\n"
            b" continuation\nX-Local-Graph-Import-ID: second\n"
            b"Content-Type: multipart/mixed; boundary=z\n\n"
            b"--z\nContent-Transfer-Encoding: base64\n\nAAECAw==\n--z--\n"
        )
        result = app.prepare_message(raw, "our-marker")
        self.assertEqual(result.lower().count(b"x-local-graph-import-id:"), 1)
        self.assertIn(b"our-marker\r\n", result)
        self.assertNotIn(b"forged", result)
        self.assertNotIn(b"continuation", result)
        self.assertEqual(result.partition(b"\r\n\r\n")[2], raw.partition(b"\n\n")[2].replace(b"\n", b"\r\n"))

    def test_remote_disappearance_is_held_without_blocking_next_message(self):
        app.save_page(self.db, page("gone", "available"))
        graph = Mock()
        graph.get.side_effect = [app.GraphFailure(404), MIME]
        original_deliver = app.deliver
        with patch.object(app, "deliver", side_effect=lambda db, row, msg, config: original_deliver(
            db, row, msg, config, FakeLMTP
        )):
            count, errors, _ = app.deliver_pending(self.db, graph, self.config)
        self.assertEqual((count, errors), (1, 1))
        self.assertEqual(self.row("gone")["state"], "failed")
        self.assertEqual(self.row("available")["state"], "delivered")

    def test_run_imports_once_across_delta_pages_and_subsequent_runs(self):
        graph = Mock()
        graph.get.side_effect = [
            {"id": "user"}, page("one", more=True), MIME, page("one"),
            {"id": "user"}, page("one"),
        ]
        original_deliver = app.deliver
        with patch.object(app, "deliver", side_effect=lambda db, row, msg, config: original_deliver(
            db, row, msg, config, FakeLMTP
        )):
            self.assertEqual(app.run(self.db, graph, self.config), 0)
            self.assertEqual(app.run(self.db, graph, self.config), 0)
        self.assertEqual(len(FakeLMTP.messages), 1)

    def test_temporary_rejection_retries_only_on_next_run_not_every_page(self):
        graph = Mock()
        graph.get.side_effect = [
            {"id": "user"}, page("one", more=True), MIME, page("one"),
            {"id": "user"}, MIME, page("one"),
        ]
        original_deliver = app.deliver
        with patch.object(app, "deliver", side_effect=lambda db, row, msg, config: original_deliver(
            db, row, msg, config, FakeLMTP
        )):
            FakeLMTP.reply = 451
            self.assertEqual(app.run(self.db, graph, self.config), 1)
            self.assertEqual(len(FakeLMTP.messages), 1)
            FakeLMTP.reply = 250
            self.assertEqual(app.run(self.db, graph, self.config), 0)
        self.assertEqual(len(FakeLMTP.messages), 2)

    def test_expired_delta_retains_delivered_history(self):
        app.save_page(self.db, page("one"))
        self.deliver()
        graph = Mock()
        graph.get.side_effect = [{"id": "user"}, app.GraphFailure(410)]
        with self.assertRaises(app.ImportFailure):
            app.run(self.db, graph, self.config)
        self.assertIsNone(app.metadata(self.db, "cursor"))
        self.assertEqual(self.row()["state"], "delivered")

    def test_retry_deadline_defers_network_access(self):
        with self.db:
            app.put_metadata(self.db, "retry_at", time.time() + 3600)
        graph = Mock()
        self.assertEqual(app.run(self.db, graph, self.config), 0)
        graph.get.assert_not_called()

    def test_foreign_delta_link_rolls_back_without_sending_credentials(self):
        bad = page("one")
        bad["@odata.deltaLink"] = "https://example.org/stolen"
        with self.assertRaises(app.ImportFailure):
            app.save_page(self.db, bad)
        self.assertIsNone(self.row())
        graph = app.Graph(self.config)
        graph.access_token = Mock()
        with self.assertRaises(app.ImportFailure):
            graph.get("https://example.org/")
        graph.access_token.assert_not_called()

    def test_token_refresh_and_immutable_header_on_each_request(self):
        graph = app.Graph(self.config)
        graph.access_token = Mock(side_effect=["old-token", "new-token"])
        response = Mock()
        response.read.return_value = b'{"id":"user"}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        graph.opener = Mock()
        graph.opener.open.side_effect = [
            HTTPError(app.GRAPH + "/me", 401, "unauthorized", {}, io.BytesIO()),
            response,
        ]
        self.assertEqual(graph.get(app.GRAPH + "/me"), {"id": "user"})
        requests = [call.args[0] for call in graph.opener.open.call_args_list]
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer old-token")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer new-token")
        for request in requests:
            self.assertIn('IdType="ImmutableId"', request.get_header("Prefer"))

    def test_throttle_deadline_is_persisted_by_cli(self):
        # Release our lock before invoking the same application's CLI entry point.
        self.store.__exit__(None, None, None)
        self.store = app.locked_store(self.config.state_dir)
        graph = Mock()
        deadline = int(time.time()) + 600
        graph.get.side_effect = app.GraphFailure(429, deadline)
        with patch.object(app.Config, "load", return_value=self.config), patch.object(app, "Graph", return_value=graph):
            self.assertEqual(app.main(["--config", "/unused", "run"]), 1)
        self.db = self.store.__enter__()
        self.assertEqual(float(app.metadata(self.db, "retry_at")), deadline)
        # Cleanup was registered against the original context manager.
        self.addCleanup(self.store.__exit__, None, None, None)

    def test_overlapping_run_cannot_open_another_ledger_session(self):
        with app.locked_store(self.config.state_dir) as other:
            self.assertIsNone(other)

    def test_operator_resolution_is_required_before_retrying_uncertain_delivery(self):
        app.save_page(self.db, page("one"))
        app.change_state(self.db, "one", "sending")
        self.store.__exit__(None, None, None)
        with patch.object(app.Config, "load", return_value=self.config):
            output = io.StringIO()
            with patch("sys.stdout", output):
                self.assertEqual(app.main(["--config", "/unused", "status"]), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["held"][0]["remote_id"], "one")
            self.assertEqual(app.main(["--config", "/unused", "resolve", "one", "--as", "pending"]), 0)
            self.assertEqual(app.main(["--config", "/unused", "resolve", "one", "--as", "delivered"]), 1)
        self.store = app.locked_store(self.config.state_dir)
        self.db = self.store.__enter__()
        self.addCleanup(self.store.__exit__, None, None, None)
        self.assertEqual(self.row()["state"], "pending")

    def test_example_config_and_size_limit(self):
        config = app.Config.load(Path(__file__).with_name("config.example.toml"))
        self.assertTrue(config.token_command)
        small_config = app.Config(self.config.state_dir, ["/unused"], "/unused", "alice", max_message_bytes=16)
        graph = app.Graph(small_config)
        graph.token = "token"
        response = Mock()
        response.read.return_value = b"x" * 17
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        graph.opener = Mock()
        graph.opener.open.return_value = response
        with self.assertRaisesRegex(app.ImportFailure, "size limit"):
            graph.get(app.GRAPH + "/me/messages/one/$value", mime=True)

    def test_real_unix_lmtp_roundtrip_dot_stuffing_and_final_response(self):
        path = str(self.directory / "lmtp.sock")
        received = []
        server_errors = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            listener.listen(1)
            listener.settimeout(5)

            def serve():
                try:
                    conn, _ = listener.accept()
                    with conn:
                        conn.settimeout(5)
                        with conn.makefile("rwb", buffering=0) as stream:
                            stream.write(b"220 local LMTP\r\n")
                            self.assertTrue(stream.readline().startswith(b"lhlo "))
                            stream.write(b"250-local\r\n250 8BITMIME\r\n")
                            self.assertEqual(stream.readline(), b"mail FROM:<>\r\n")
                            stream.write(b"250 sender\r\n")
                            self.assertEqual(stream.readline(), b"rcpt TO:<alice>\r\n")
                            stream.write(b"250 recipient\r\n")
                            self.assertEqual(stream.readline(), b"data\r\n")
                            stream.write(b"354 go ahead\r\n")
                            lines = []
                            while True:
                                line = stream.readline()
                                if not line:
                                    raise AssertionError("Unexpected client EOF")
                                if line == b".\r\n":
                                    break
                                lines.append(line[1:] if line.startswith(b"..") else line)
                            received.append(b"".join(lines))
                            stream.write(b"250 2.0.0 stored\r\n")
                except Exception as exc:
                    server_errors.append(exc)

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            app.save_page(self.db, page("one"))
            config = app.Config(self.config.state_dir, ["/unused"], path, "alice", timeout=5)
            row = self.row()
            message = app.prepare_message(MIME, row["marker"])
            try:
                self.assertTrue(app.deliver(self.db, row, message, config))
            finally:
                thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(server_errors, [])
            self.assertEqual(received, [message])
            self.assertEqual(self.row()["state"], "delivered")


if __name__ == "__main__":
    unittest.main()
