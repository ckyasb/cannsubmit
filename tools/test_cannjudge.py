#!/usr/bin/env python3
"""Created: 2026-11-06. Author: ckyasb.
Purpose: Verify CANNJudge file overlays, session handling and submission transport.
Added: Offline multi-file, read-only, path-boundary, cookie-redaction and no-retry tests.
"""

import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import cannjudge as cj


PROBLEM_ID = "1" * 24
USER_ID = "2" * 24
SUBMISSION_ID = "3" * 24
PROBLEM = {"_id": PROBLEM_ID, "name": "dot", "code_template": "gitcode_template_noopapi"}
TEMPLATE = [cj.ProjectFile("build.sh", "immutable", False),
            cj.ProjectFile("op_host/dot.cpp", "old host"),
            cj.ProjectFile("op_kernel/dot.cpp", "old kernel"),
            cj.ProjectFile("op_kernel/dot.h", "old header")]


class ProjectTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=os.environ.get("CANNJUDGE_TEST_TMPDIR"))
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "op_kernel").mkdir()
        (self.root / "op_host").mkdir()
        (self.root / "build.sh").write_text("local immutable edit")
        (self.root / "op_kernel/dot.cpp").write_text("new kernel")
        (self.root / "op_kernel/extra.h").write_text("new dependency")

    def plan(self, **kwargs):
        return cj.build_plan(PROBLEM, TEMPLATE, self.root, **kwargs)

    def client(self):
        client = Mock()
        client.has_credentials = True
        client.account.return_value = USER_ID
        client.template.return_value = TEMPLATE
        client.request.side_effect = lambda method, path, data=None: (
            PROBLEM if method == "GET" else {"data": {"submissionId": SUBMISSION_ID}})
        return client

    def test_multiple_files_and_new_dependency_preserve_missing_files(self):
        plan = self.plan()
        files = {file["path"]: file["content"] for file in plan["files"]}
        self.assertEqual(files, {"op_host/dot.cpp": "old host", "op_kernel/dot.cpp": "new kernel",
                                 "op_kernel/dot.h": "old header", "op_kernel/extra.h": "new dependency"})
        self.assertEqual({row["path"] for row in plan["changes"]}, {"op_kernel/dot.cpp", "op_kernel/extra.h"})

    def test_selected_file_preserves_other_previous_submission_files(self):
        previous = {"_id": SUBMISSION_ID, "problem_id": PROBLEM_ID, "files": [
            {"path": "op_kernel/dot.h", "content": "previous header"},
            {"path": "op_kernel/previous.h", "content": "previous dependency"},
            {"path": "build.sh", "content": "must not overwrite immutable file"}]}
        plan = self.plan(selected=["op_kernel/dot.cpp"], previous=previous)
        files = {file["path"]: file["content"] for file in plan["files"]}
        self.assertEqual(files["op_kernel/dot.h"], "previous header")
        self.assertEqual(files["op_kernel/previous.h"], "previous dependency")
        self.assertNotIn("op_kernel/extra.h", files)
        self.assertNotIn("build.sh", files)

    def test_optional_diff_includes_changed_and_added_files(self):
        plan = self.plan(include_diff=True)
        changes = {row["path"]: row for row in plan["changes"]}
        self.assertIn("-old kernel", changes["op_kernel/dot.cpp"]["diff"])
        self.assertIn("+new kernel", changes["op_kernel/dot.cpp"]["diff"])
        self.assertIn("+new dependency", changes["op_kernel/extra.h"]["diff"])

    def test_new_header_does_not_change_first_legacy_host_source(self):
        (self.root / "op_host/a_helper.h").write_text("new helper")
        self.assertEqual(cj.legacy_payload(self.plan()["files"])["host_cpp"], "old host")

    def test_readonly_and_missing_selected_files_fail(self):
        for path in ["build.sh", "op_kernel/missing.h", "op_kernel/not_allowed.py"]:
            with self.subTest(path=path), self.assertRaises(cj.CannJudgeError):
                self.plan(selected=[path])

    def test_previous_submission_must_match_problem(self):
        with self.assertRaises(cj.CannJudgeError):
            self.plan(previous={"problem_id": "4" * 24, "files": []})

    def test_source_symlink_cannot_escape_root(self):
        (self.root / "op_kernel/escape.h").symlink_to(self.root.parent / "outside.h")
        with self.assertRaises(cj.CannJudgeError):
            self.plan(selected=["op_kernel/escape.h"])

    def test_traversal_and_duplicate_paths_fail(self):
        for path in ["/absolute.h", "../escape.h", "op_kernel/../escape.h", "op_kernel//x.h", "a\\b.h"]:
            with self.subTest(path=path), self.assertRaises(cj.CannJudgeError):
                cj.file_path(path)
        with self.assertRaises(cj.CannJudgeError):
            cj.normalize_files([{"path": "x.h"}, {"path": "x.h"}])

    def test_new_file_limit(self):
        for index in range(20):
            (self.root / "op_kernel" / f"extra_{index}.h").write_text("dependency")
        with self.assertRaises(cj.CannJudgeError):
            self.plan()

    def test_legacy_template_and_npu_file_rules(self):
        self.assertEqual(len(cj.template_files({"data": {"kernel_cpp": "kernel"}})), 4)
        self.assertFalse(cj.allows_new_file("judge.asc", "npu_kernel_dev", TEMPLATE))
        self.assertTrue(cj.allows_new_file("custom.asc", "npu_kernel_dev", TEMPLATE))
        self.assertFalse(cj.allows_new_file("custom.cpp", "npu_kernel_dev", TEMPLATE))

    def test_commit_contains_all_files_and_website_legacy_fields(self):
        client = self.client()
        result = cj.commit(client, self.plan())
        self.assertEqual(result["submission_id"], SUBMISSION_ID)
        self.assertEqual(result["user_id"], USER_ID)
        posts = [call for call in client.request.call_args_list if call.args[0] == "POST"]
        self.assertEqual(len(posts), 1)
        payload = posts[0].args[2]
        self.assertEqual(payload["userId"], USER_ID)
        self.assertEqual(payload["problemId"], PROBLEM_ID)
        self.assertEqual(payload["host_cpp"], "old host")
        self.assertEqual(payload["kernel_cpp"], "new kernel")
        self.assertEqual(len(payload["files"]), 4)

    def test_changed_template_missing_files_and_locked_file_abort_before_post(self):
        plans = [self.plan(), self.plan(), self.plan()]
        plans[0]["template_sha256"]["build.sh"] = "changed"
        plans[1]["files"] = [file for file in plans[1]["files"] if file["path"] != "op_kernel/dot.h"]
        plans[2]["files"].append({"path": "build.sh", "content": "bad"})
        for plan in plans:
            client = self.client()
            with self.subTest(plan=plans.index(plan)), self.assertRaises(cj.CannJudgeError):
                cj.commit(client, plan)
            self.assertFalse(any(call.args[0] == "POST" for call in client.request.call_args_list))

    def test_commit_rejects_wrong_origin_type_and_missing_session(self):
        for changes in [{"origin": "https://example.invalid"}, {"template_type": "npu_kernel_dev"}]:
            plan = self.plan()
            plan.update(changes)
            with self.assertRaises(cj.CannJudgeError):
                cj.commit(self.client(), plan)
        client = self.client()
        client.has_credentials = False
        with self.assertRaises(cj.CannJudgeError):
            cj.commit(client, self.plan())

    def test_output_does_not_overwrite_existing_plan(self):
        path = self.root / "plan.json"
        cj.write_json(path, {"original": True})
        with self.assertRaises(cj.CannJudgeError):
            cj.write_json(path, {"original": False})
        self.assertEqual(json.loads(path.read_text()), {"original": True})


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=os.environ.get("CANNJUDGE_TEST_TMPDIR"))
        self.addCleanup(self.directory.cleanup)
        self.history = Path(self.directory.name) / "scores.jsonl"
        self.context = dict(problem_id=PROBLEM_ID, user_id=USER_ID, submission_id=SUBMISSION_ID)

    def row(self, submission_id=SUBMISSION_ID, score=76.73):
        return {"user_id": USER_ID, "submission_id": submission_id, "score": score, "rank": 11,
                "theory_score": 0, "result": [{"score": 0}],
                "submission": {"_id": submission_id, "ID": 123, "status": "Pass"}}

    def response(self, row=None, **pagination):
        return {"rows": [self.row() if row is None else row], "pages": 1, "current": 1, **pagination}

    def snapshot(self, response):
        with patch.object(cj.Client, "request", return_value=response) as request:
            snapshot = cj.score_snapshot(**self.context)
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertIn(f"/api/problems/{PROBLEM_ID}/ranking?", request.call_args.args[1])
        return snapshot

    def test_score_uses_ranking_fields_not_theory_or_testcase_defaults(self):
        snapshot = self.snapshot(self.response())
        self.assertEqual(snapshot["submission_score"], 76.73)
        self.assertEqual(snapshot["submission_rank"], 11)
        self.assertEqual(snapshot["score_state"], "matched")
        self.assertFalse(snapshot["cookie_used"])

    def test_historical_best_is_not_attributed_to_requested_submission(self):
        snapshot = self.snapshot(self.response(self.row("4" * 24)))
        self.assertEqual(snapshot["submission_id"], SUBMISSION_ID)
        self.assertIsNone(snapshot["submission_score"])
        self.assertIsNone(snapshot["submission_rank"])
        self.assertEqual(snapshot["leaderboard"]["submission_id"], "4" * 24)
        self.assertEqual(snapshot["leaderboard"]["score"], 76.73)
        self.assertEqual(snapshot["score_state"], "ranking_uses_other_submission")

    def test_actual_zero_is_preserved_only_for_a_matching_submission(self):
        self.assertEqual(self.snapshot(self.response(self.row(score=0)))["submission_score"], 0)

    def test_pagination_matches_user_id_instead_of_display_name(self):
        other = self.row()
        other.update(user_id="4" * 24, nickname=USER_ID)
        pages = [self.response(other, pages=2), self.response(current=2, pages=2)]
        with patch.object(cj.Client, "request", side_effect=pages) as request:
            snapshot = cj.score_snapshot(**self.context)
        self.assertEqual(snapshot["submission_score"], 76.73)
        self.assertIn("page=2", request.call_args.args[1])

    def test_missing_user_is_null_not_zero(self):
        snapshot = self.snapshot({"rows": [], "pages": 0, "current": 1})
        self.assertEqual(snapshot["score_state"], "not_ranked")
        self.assertIsNone(snapshot["submission_score"])
        self.assertIsNone(snapshot["leaderboard"])

    def test_missing_or_invalid_score_and_rank_are_not_guessed(self):
        for changes in ({"score": None}, {"score": float("nan")}, {"score": True},
                        {"score": "76.73"}, {"rank": 0}, {"rank": True},
                        {"submission": {"_id": "4" * 24}}):
            row = self.row()
            row.update(changes)
            with self.subTest(changes=changes):
                snapshot = self.snapshot(self.response(row))
                self.assertEqual(snapshot["score_state"], "unavailable")
                self.assertIsNone(snapshot["submission_score"])
                self.assertIsNone(snapshot["leaderboard"])

    def test_protocol_or_network_failure_returns_an_explicit_unavailable_record(self):
        for response in ({}, {"rows": [], "pages": 2, "current": 1},
                         {"rows": [], "pages": 1, "current": 2}):
            with self.subTest(response=response):
                self.assertEqual(self.snapshot(response)["score_state"], "unavailable")
        with patch.object(cj.Client, "request", side_effect=cj.CannJudgeError("connection failed")):
            snapshot = cj.score_snapshot(**self.context)
        self.assertEqual(snapshot["reason"], "connection failed")
        self.assertIsNone(snapshot["submission_score"])

    def test_receipt_requires_explicit_missing_identity_and_rejects_conflicts(self):
        receipt = dict(self.context)
        del receipt["user_id"]
        self.assertEqual(cj.score_context(receipt, user_id=USER_ID), self.context)
        with self.assertRaisesRegex(cj.CannJudgeError, "user_id"):
            cj.score_context(receipt)
        with self.assertRaisesRegex(cj.CannJudgeError, "conflicts"):
            cj.score_context(self.context, submission_id="4" * 24)

    def test_history_is_append_only_and_keeps_distinct_requested_and_adopted_ids(self):
        first = self.snapshot(self.response(self.row("4" * 24)))
        second = self.snapshot(self.response())
        cj.append_history(self.history, first)
        cj.append_history(self.history, second)
        records = [json.loads(line) for line in self.history.read_text().splitlines()]
        self.assertEqual(records, [first, second])
        self.assertIn("queried_at_utc", records[0])
        self.assertIn("source_url", records[0])

    def test_score_cli_ignores_cookie_file_and_environment_without_private_requests(self):
        output = io.StringIO()
        with patch.dict(os.environ, CANNJUDGE_COOKIE_FILE="/nonexistent/session"), \
                patch.object(cj, "load_cookies", side_effect=AssertionError("cookie access")), \
                patch.object(cj.Client, "request", return_value=self.response()) as request, \
                contextlib.redirect_stdout(output):
            code = cj.main(["--cookie-file", "/also/not/read", "score", "--id", SUBMISSION_ID,
                            "--problem-id", PROBLEM_ID, "--user-id", USER_ID, "--history", str(self.history)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["submission_score"], 76.73)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("/api/submissions/", request.call_args.args[1])

    def test_commit_saves_receipt_before_lookup_and_preserves_id_after_query_failure(self):
        output, events = io.StringIO(), []

        def save(path, record):
            events.append(record["record_type"])

        def score(**context):
            self.assertEqual(events, ["submission_receipt"])
            return {"record_type": "score", "score_state": "unavailable", "submission_score": None}

        with patch.object(cj, "commit", return_value=dict(self.context)), \
                patch.object(cj, "read_json", return_value={}), \
                patch.object(cj, "append_history", side_effect=save), \
                patch.object(cj, "score_snapshot", side_effect=score), contextlib.redirect_stdout(output):
            self.assertEqual(cj.main(["commit", "--plan", "unused", "--history", str(self.history)]), 0)
        self.assertEqual(events, ["submission_receipt", "score"])
        self.assertEqual(json.loads(output.getvalue())["submission_id"], SUBMISSION_ID)

    def test_history_failure_after_post_keeps_accepted_id_visible_and_does_not_retry(self):
        output = io.StringIO()
        with patch.object(cj, "commit", return_value=dict(self.context)) as commit, \
                patch.object(cj, "read_json", return_value={}), \
                patch.object(cj, "score_snapshot", return_value={"record_type": "score"}), \
                patch.object(cj, "append_history", side_effect=cj.CannJudgeError("disk full")), \
                contextlib.redirect_stdout(output):
            code = cj.main(["commit", "--plan", "unused", "--history", str(self.history)])
        self.assertEqual(code, 1)
        self.assertEqual(commit.call_count, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result["submission_id"], SUBMISSION_ID)
        self.assertEqual(result["history_error"], "disk full")

    def test_status_joins_numeric_id_and_status_with_anonymous_score(self):
        submission = {"_id": SUBMISSION_ID, "ID": 123, "status": "Pass", "valid": True,
                      "problem_id": PROBLEM_ID, "user_id": USER_ID, "score": 0, "theory_score": 0}
        output = io.StringIO()
        with patch.object(cj.Client, "submission", return_value=submission), \
                patch.object(cj.Client, "request", return_value=self.response()), \
                contextlib.redirect_stdout(output):
            self.assertEqual(cj.main(["status", "--id", SUBMISSION_ID, "--history", str(self.history)]), 0)
        result = json.loads(output.getvalue())
        self.assertNotIn("score", result)
        self.assertEqual(result["score_snapshot"]["submission_score"], 76.73)
        saved = json.loads(self.history.read_text())
        self.assertEqual(saved["submission_number"], 123)
        self.assertEqual(saved["submission_status"], "Pass")

    def test_status_without_identity_fields_preserves_status_and_records_null_score(self):
        output = io.StringIO()
        with patch.object(cj.Client, "submission", return_value={"ID": 123, "status": "Pass"}), \
                patch.object(cj.Client, "request") as request, contextlib.redirect_stdout(output):
            self.assertEqual(cj.main(["status", "--id", SUBMISSION_ID, "--history", str(self.history)]), 0)
        request.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["status"], "Pass")
        saved = json.loads(self.history.read_text())
        self.assertEqual(saved["submission_id"], SUBMISSION_ID)
        self.assertIsNone(saved["submission_score"])
        self.assertEqual(saved["score_state"], "unavailable")


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=os.environ.get("CANNJUDGE_TEST_TMPDIR"))
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cookie.txt"

    def test_raw_cookie_and_redaction(self):
        self.path.write_text("Cookie: cannjudge_auth=synthetic-secret\n")
        client = cj.Client(self.path)
        self.assertEqual(client.printable({"result": ["echo synthetic-secret"]}),
                         {"result": ["echo <redacted>"]})

    def test_bare_signed_session_value_uses_the_verified_cookie_name(self):
        self.path.write_text("synthetic-payload.synthetic-signature")
        _, header = cj.load_cookies(self.path)
        self.assertEqual(header, "cannjudge_auth=synthetic-payload.synthetic-signature")

    def test_browser_json_excludes_other_domains(self):
        self.path.write_text(json.dumps({"cookies": [
            {"domain": ".cannjudge.cn", "name": "cannjudge_auth", "value": "synthetic-secret", "expires": -1},
            {"domain": "example.invalid", "name": "other", "value": "unrelated"}]}))
        jar, header = cj.load_cookies(self.path)
        self.assertIsNone(header)
        self.assertEqual([cookie.name for cookie in jar], ["cannjudge_auth"])

    def test_netscape_export_supports_httponly(self):
        self.path.write_text("# Netscape HTTP Cookie File\n"
                             "#HttpOnly_cannjudge.cn\tFALSE\t/\tTRUE\t0\tcannjudge_auth\tsynthetic-secret\n")
        jar, _ = cj.load_cookies(self.path)
        self.assertEqual([cookie.value for cookie in jar], ["synthetic-secret"])

    def test_malformed_cookie_diagnostics_do_not_echo_input(self):
        for text in ['[{"value":"synthetic-secret"}', '# Netscape HTTP Cookie File\nsynthetic-secret\n',
                     'cannjudge_auth=synthetic-secret\r\nInjected: header']:
            self.path.write_text(text)
            capture = io.StringIO()
            with contextlib.redirect_stderr(capture), self.assertRaises(cj.CannJudgeError) as raised:
                cj.load_cookies(self.path)
            self.assertNotIn("synthetic-secret", str(raised.exception) + capture.getvalue())

    def test_session_identity_is_verified_by_server(self):
        encoded = base64.urlsafe_b64encode(json.dumps({"uid": USER_ID}).encode()).decode().rstrip("=")
        self.path.write_text("cannjudge_auth=" + encoded + ".synthetic-signature")
        client = cj.Client(self.path)
        client.request = Mock(side_effect=[{"ID": 12}, {"_id": USER_ID}])
        self.assertEqual(client.account(), USER_ID)
        self.assertEqual(client.request.call_args.args[1], "/api/users/me/12")


class TransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                return

            def do_GET(self):
                cls.requests.append((self.path, self.headers.get("Cookie")))
                if self.path == "/api/redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://example.invalid/collect")
                    self.end_headers()
                    return
                if self.path.startswith("/api/public"):
                    self.send_response(200)
                    self.send_header("Set-Cookie", "test_session=synthetic-secret; Path=/")
                    self.end_headers()
                    self.wfile.write(b'{"ok":true}')
                    return
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"synthetic-secret"}')

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_http_cookie_transport_redirect_block_and_error_redaction(self):
        client = cj.Client()
        client._cookie_header = "cannjudge_auth=synthetic-secret"
        client._cookie_values = ["synthetic-secret"]
        with patch.object(cj, "ORIGIN", f"http://127.0.0.1:{self.server.server_port}"):
            for path in ["/api/denied", "/api/redirect"]:
                with self.subTest(path=path), self.assertRaises(cj.CannJudgeError) as raised:
                    client.request("GET", path)
                self.assertNotIn("synthetic-secret", str(raised.exception))
        self.assertEqual(self.requests[-1], ("/api/redirect", "cannjudge_auth=synthetic-secret"))

    def test_post_timeout_is_not_retried(self):
        client = cj.Client()
        client.opener = Mock()
        client.opener.open.side_effect = TimeoutError()
        with self.assertRaisesRegex(cj.CannJudgeError, "uncertain"):
            client.request("POST", "/api/submissions/submit", {"files": []})
        self.assertEqual(client.opener.open.call_count, 1)

    def test_anonymous_transport_never_replays_server_cookies(self):
        client = cj.Client("/not/read", anonymous=True)
        with patch.object(cj, "ORIGIN", f"http://127.0.0.1:{self.server.server_port}"):
            client.request("GET", "/api/public/page1")
            client.request("GET", "/api/public/page2")
        self.assertEqual(self.requests[-2:], [("/api/public/page1", None), ("/api/public/page2", None)])

    def test_other_origins_are_rejected_before_network(self):
        client = cj.Client()
        client.opener = Mock()
        for path in ["https://example.invalid/api/steal", "//example.invalid/api/steal", "/outside"]:
            with self.assertRaises(cj.CannJudgeError):
                client.request("GET", path)
        client.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
