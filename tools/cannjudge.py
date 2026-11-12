#!/usr/bin/env python3
"""Created: 2026-11-06. Author: ckyasb.
Purpose: Submit multi-file CANNJudge projects using an existing website session.
Added: Multi-file submission plans and queries; anonymous ranking/history added 2026-11-11.
"""

import argparse
import base64
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import difflib
import hashlib
import http.cookiejar
import http.cookies
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import warnings
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener


ORIGIN = "https://cannjudge.cn"
DEFAULT_CONTEST = ORIGIN + "/public/asic-op-s10"
SOURCE_DIRS = {"op_host", "op_kernel", "op_api"}
DEFAULT_HISTORY = Path(__file__).resolve().parent.parent / "reports" / "cannjudge_scores.jsonl"
LEGACY_FIELDS = {"tiling.h": "tiling_h", "tiling_key.h": "tiling_key_h",
                 "host.cpp": "host_cpp", "kernel.cpp": "kernel_cpp"}


class CannJudgeError(Exception):
    """A diagnostic that is safe to display without credentials or response bodies."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # API calls must not forward an explicit Cookie header to a redirected host.
        raise CannJudgeError(f"API returned a redirect (HTTP {code}); check the session and site")


def file_path(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise CannJudgeError("Invalid project-relative file path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise CannJudgeError("Project file paths must be normalized and relative")
    return value


def object_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{24}", value):
        raise CannJudgeError("Expected a CANNJudge object ID (24 hexadecimal characters)")
    return value


def content_hash(content):
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ProjectFile:
    path: str
    content: str
    editable: bool = True


def normalize_files(items):
    if not isinstance(items, list):
        raise CannJudgeError("The website did not return a project file list")
    files, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise CannJudgeError("Invalid project file record")
        path = file_path(item.get("path") or item.get("key") or item.get("label"))
        if path in seen:
            raise CannJudgeError(f"Duplicate project path: {path}")
        if not isinstance(item.get("content", ""), str):
            raise CannJudgeError(f"Expected UTF-8 text content: {path}")
        seen.add(path)
        files.append(ProjectFile(path, item.get("content", ""), item.get("editable") is not False))
    return files


def template_files(response):
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise CannJudgeError("Unexpected template response; website protocol may have changed")
    data = response["data"]
    if data.get("files"):
        return normalize_files(data["files"])
    if not any(field in data for field in LEGACY_FIELDS.values()):
        raise CannJudgeError("Template contains no files")
    return normalize_files([{"path": path, "content": data.get(field, "")}
                            for path, field in LEGACY_FIELDS.items()])


def allows_new_file(path, template_type, template):
    path = file_path(path)
    if template_type == "npu_kernel_dev":
        return ("/" not in path and path not in {"judge.asc", "data_utils.h", "main.asc"}
                and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(asc|h)", path) is not None
                and len(path) <= 128)
    parts = path.split("/")
    return (len(parts) == 2 and parts[0] in SOURCE_DIRS
            and any(file.path.startswith(parts[0] + "/") for file in template)
            and len(parts[1]) <= 128
            and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.(h|cpp)", parts[1]) is not None)


def load_cookies(path):
    """Read only an explicitly supplied export; never persist or print session values."""
    jar = http.cookiejar.CookieJar()
    if path is None:
        return jar, None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
        if text.startswith("#"):
            exported = http.cookiejar.MozillaCookieJar(str(path))
            # The stdlib loader may warn with the malformed input line included.
            with warnings.catch_warnings(record=True), contextlib.redirect_stderr(io.StringIO()):
                exported.load(ignore_discard=True, ignore_expires=True)
            for cookie in exported:
                # curl/browser Netscape exports use zero for session-only expiry.
                if cookie.expires == 0:
                    cookie.expires, cookie.discard = None, True
                if cookie.domain.lstrip(".") == "cannjudge.cn" and not cookie.is_expired():
                    jar.set_cookie(cookie)
            if not list(jar):
                raise ValueError("No applicable cookies")
            return jar, None
        if text.startswith(("[", "{")):
            exported = json.loads(text)
            items = exported.get("cookies") if isinstance(exported, dict) else exported
            if not isinstance(items, list):
                raise ValueError("Not a browser cookie export")
            for item in items:
                domain = str(item.get("domain", "")).lower()
                if domain.lstrip(".") != "cannjudge.cn":
                    continue
                name, value = item["name"], item["value"]
                if not isinstance(name, str) or not isinstance(value, str):
                    raise ValueError("Cookie must contain strings")
                if any(ord(char) < 32 or ord(char) == 127 for char in name + value):
                    raise ValueError("Cookie contains a control character")
                expiry = item.get("expirationDate", item.get("expires"))
                expiry = int(expiry) if expiry is not None and float(expiry) > 0 else None
                jar.set_cookie(http.cookiejar.Cookie(
                    version=0, name=name, value=value, port=None, port_specified=False,
                    domain=domain, domain_specified=not item.get("hostOnly", not domain.startswith(".")),
                    domain_initial_dot=domain.startswith("."), path=item.get("path") or "/",
                    path_specified=True, secure=bool(item.get("secure", False)),
                    expires=expiry, discard=expiry is None, comment=None, comment_url=None,
                    rest={"HttpOnly": None} if item.get("httpOnly") else {}, rfc2109=False))
            if not list(jar):
                raise ValueError("No applicable cookies")
            return jar, None
        header = text.removeprefix("Cookie:").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+={0,2}\.[A-Za-z0-9_-]+={0,2}", header):
            header = "cannjudge_auth=" + header
        parsed = http.cookies.SimpleCookie()
        parsed.load(header)
        if not parsed or any(ord(char) < 32 or ord(char) == 127 for char in header):
            raise ValueError("Invalid Cookie header")
        return jar, header
    except (OSError, ValueError, TypeError, KeyError, AttributeError, http.cookies.CookieError):
        # Parsing errors can include cookie contents, including malformed JSON excerpts.
        raise CannJudgeError("Cannot load Cookie file; use Netscape, browser JSON or a Cookie header") from None


class Client:
    def __init__(self, cookie_file=None, timeout=30, *, anonymous=False):
        # Public score reads must neither load a session nor replay server Set-Cookie.
        jar, self._cookie_header = ((http.cookiejar.CookieJar(), None) if anonymous
                                    else load_cookies(cookie_file))
        self.has_credentials = cookie_file is not None and not anonymous
        self._cookie_values = [cookie.value for cookie in jar]
        if self._cookie_header:
            parsed = http.cookies.SimpleCookie()
            parsed.load(self._cookie_header)
            self._cookie_values.extend(cookie.value for cookie in parsed.values())
        self.timeout = timeout
        self.opener = (build_opener(NoRedirect()) if anonymous
                       else build_opener(NoRedirect(), HTTPCookieProcessor(jar)))

    def printable(self, data):
        """Redact session values even if an unexpected server response echoes them."""
        if isinstance(data, dict):
            return {self.printable(key): self.printable(value) for key, value in data.items()}
        if isinstance(data, list):
            return [self.printable(value) for value in data]
        if isinstance(data, str):
            for value in sorted(set(self._cookie_values), key=len, reverse=True):
                if value:
                    data = data.replace(value, "<redacted>")
        return data

    def account(self):
        # The site's signed session stores uid in its first URL-safe base64 component.
        # Identity decoded here is only a hint; /users/me still verifies the server signature.
        for value in self._cookie_values:
            try:
                encoded = value.split(".")[0]
                decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
                uid = decoded.get("uid")
            except (ValueError, UnicodeError, AttributeError):
                continue
            if isinstance(uid, str) and re.fullmatch(r"[0-9a-fA-F]{24}", uid):
                public = self.request("GET", "/api/users/object/" + uid)
                numeric_id = public.get("ID")
                if not isinstance(numeric_id, int):
                    raise CannJudgeError("Cannot resolve the session account")
                account = self.request("GET", "/api/users/me/" + str(numeric_id))
                if account.get("_id") != uid:
                    raise CannJudgeError("Session account identity mismatch")
                return uid
        raise CannJudgeError("Cookie file contains no recognizable CANNJudge signed session")

    def request(self, method, path, data=None):
        if not path.startswith("/api/") or urlsplit(path).netloc or "\r" in path or "\n" in path:
            raise CannJudgeError("Only same-origin CANNJudge API paths are supported")
        headers = {"Accept": "application/json", "Referer": DEFAULT_CONTEST}
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        payload = None
        if data is not None:
            headers.update({"Content-Type": "application/json", "Origin": ORIGIN})
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        request = Request(ORIGIN + path, data=payload, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            reason = ""
            try:
                body = json.load(error)
                for item in (body, body.get("data", {})) if isinstance(body, dict) else ():
                    if isinstance(item, dict):
                        message = next((item[key] for key in ("message", "msg", "error", "detail")
                                        if isinstance(item.get(key), str) and item[key]), "")
                        if message:
                            reason = ": " + self.printable(message)[:800]
                            break
            except (ValueError, UnicodeError, OSError):
                reason = ""
            hint = ""
            if error.code in {401, 403}:
                hint = "; check Cookie expiry and contest permissions"
            elif error.code == 429:
                hint = "; rate limited, retry later"
            raise CannJudgeError(f"{method} {path}: HTTP {error.code}{reason}{hint}") from None
        except (TimeoutError, URLError, OSError):
            hint = "; submission outcome is uncertain: check website records before retrying" if method == "POST" else ""
            raise CannJudgeError(f"{method} {path}: connection failed or timed out{hint}") from None
        except (ValueError, UnicodeError):
            hint = "; check website records before retrying" if method == "POST" else ""
            raise CannJudgeError(f"{method} {path}: invalid JSON response{hint}") from None

    def problems(self, contest_url):
        url = urlsplit(contest_url)
        parts = url.path.strip("/").split("/")
        if (url.scheme != "https" or url.netloc != "cannjudge.cn" or url.query or url.fragment
                or len(parts) != 2 or parts[0] != "public" or not parts[1]):
            raise CannJudgeError("Expected https://cannjudge.cn/public/<contest-name>")
        contest = self.request("GET", "/api/contests/name/" + quote(parts[1], safe=""))
        contest_id = object_id(contest.get("_id") or contest.get("id"))
        problems = self.request("GET", "/api/problems/contest/" + contest_id)
        if not isinstance(problems, list):
            raise CannJudgeError("Unexpected contest problem list")
        return problems

    def problem(self, contest_url, token):
        matches = [problem for problem in self.problems(contest_url)
                   if token in {str(problem.get(key, "")) for key in ("_id", "id", "ID", "name", "canonical_name")}]
        if len(matches) != 1:
            raise CannJudgeError(f"Expected exactly one problem matching: {token}")
        return matches[0]

    def template(self, problem_id):
        return template_files(self.request("GET", f"/api/problems/{object_id(problem_id)}/template"))

    def submission(self, submission_id):
        return self.request("GET", "/api/submissions/" + object_id(submission_id))


def local_content(root, path):
    candidate = root / file_path(path)
    if not candidate.resolve().is_relative_to(root):
        raise CannJudgeError(f"Project file escapes source directory: {path}")
    if not candidate.exists():
        return None
    try:
        return candidate.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        raise CannJudgeError(f"Cannot read UTF-8 project file: {path}") from None


def build_plan(problem, template, source, selected=None, previous=None, include_diff=False):
    root = Path(source).resolve()
    if not root.is_dir():
        raise CannJudgeError("Source directory does not exist")
    template_type = problem.get("code_template", "")
    base = {file.path: file for file in template}
    previous_id = None
    if previous is not None:
        previous_problem = previous.get("problem_id") or (previous.get("problem") or {}).get("_id")
        if previous_problem != problem["_id"]:
            raise CannJudgeError("Previous submission belongs to another problem")
        previous_id = object_id(previous.get("_id") or previous.get("id"))
        for file in normalize_files(previous.get("files", [])):
            if file.path in base and base[file.path].editable:
                base[file.path] = ProjectFile(file.path, file.content)
            elif file.path not in base and allows_new_file(file.path, template_type, template):
                base[file.path] = file
    selected = set(selected) if selected is not None else None
    if selected is not None:
        for path in selected:
            file_path(path)
            if path in base and not base[path].editable:
                raise CannJudgeError(f"Template file is read-only: {path}")
            if path not in base and not allows_new_file(path, template_type, template):
                raise CannJudgeError(f"New file is not allowed by the website: {path}")
            if local_content(root, path) is None:
                raise CannJudgeError(f"Selected local file is missing: {path}")
    else:
        selected = {file.path for file in base.values() if file.editable}
        directories = [root] if template_type == "npu_kernel_dev" else [root / folder for folder in sorted(SOURCE_DIRS)]
        for directory in directories:
            if directory.is_dir():
                for candidate in sorted(directory.iterdir()):
                    relative = candidate.relative_to(root).as_posix()
                    if candidate.is_file() and relative not in base and allows_new_file(relative, template_type, template):
                        selected.add(relative)
    files, changes = [], []
    # Keep template order, like the editor: legacy host/kernel fields use its first source.
    for path in list(base) + sorted(selected - base.keys()):
        before = base.get(path)
        if before is not None and not before.editable:
            continue
        content = local_content(root, path) if path in selected else None
        content = before.content if content is None and before is not None else content
        if content is None:
            raise CannJudgeError(f"Missing content for new file: {path}")
        files.append({"path": path, "content": content})
        if before is None or before.content != content:
            change = {"path": path, "change": "added" if before is None else "modified",
                      "before_sha256": content_hash(before.content) if before else None,
                      "after_sha256": content_hash(content)}
            if include_diff:
                change["diff"] = "".join(difflib.unified_diff(
                    (before.content if before else "").splitlines(keepends=True),
                    content.splitlines(keepends=True), fromfile="before/" + path, tofile="after/" + path))
            changes.append(change)
    if sum(file["path"] not in {item.path for item in template} for file in files) > 20:
        raise CannJudgeError("The website allows at most 20 user-created files")
    return {"schema_version": 1, "origin": ORIGIN, "problem_id": object_id(problem["_id"]),
            "problem_name": problem.get("canonical_name") or problem.get("name"),
            "template_type": template_type, "base_submission_id": previous_id,
            "template_sha256": {file.path: content_hash(file.content) for file in template},
            "files": files, "changes": changes}


def legacy_payload(files):
    def first(predicate):
        return next((file["content"] for file in files if predicate(file["path"], PurePosixPath(file["path"]).name)), "")
    return {
        "tiling_h": first(lambda path, name: name == "tiling.h" or path.startswith("op_kernel/") and name.endswith("_tiling.h")),
        "tiling_key_h": first(lambda path, name: name == "tiling_key.h" or path.startswith("op_kernel/") and (name.startswith("tiling_key_") or name.endswith("_tiling_key.h"))),
        "host_cpp": first(lambda path, name: path.startswith("op_host/") and re.search(r"\.(cc|cpp|cxx|h|hpp)$", name, re.I)) or first(lambda path, name: name == "host.cpp"),
        "kernel_cpp": first(lambda path, name: path.startswith("op_kernel/") and re.search(r"\.(cc|cpp|cxx)$", name, re.I)) or first(lambda path, name: name == "kernel.cpp"),
    }


def commit(client, plan):
    if not client.has_credentials:
        raise CannJudgeError("commit requires --cookie-file or CANNJUDGE_COOKIE_FILE")
    if not isinstance(plan, dict) or plan.get("schema_version") != 1 or plan.get("origin") != ORIGIN:
        raise CannJudgeError("Unsupported plan format or origin")
    problem_id = object_id(plan.get("problem_id"))
    problem = client.request("GET", "/api/problems/" + problem_id)
    if problem.get("code_template", "") != plan.get("template_type", ""):
        raise CannJudgeError("Problem template type changed; create a new plan")
    template = client.template(problem_id)
    if plan.get("template_sha256") != {file.path: content_hash(file.content) for file in template}:
        raise CannJudgeError("Website template changed; create a new plan before submitting")
    files = normalize_files(plan.get("files"))
    editable = {file.path for file in template if file.editable}
    locked = {file.path for file in template if not file.editable}
    paths = {file.path for file in files}
    if not editable.issubset(paths):
        raise CannJudgeError("Plan omits editable template files; recreate the plan")
    for file in files:
        if file.path in locked or file.path not in editable and not allows_new_file(file.path, plan.get("template_type"), template):
            raise CannJudgeError(f"Cannot submit a read-only or disallowed file: {file.path}")
    if len(paths - editable) > 20:
        raise CannJudgeError("Too many user-created files")
    payload_files = [{"path": file.path, "content": file.content} for file in files]
    payload = {"problemId": problem_id, "userId": client.account(), "files": payload_files,
               **legacy_payload(payload_files)}
    # No POST retries: a timeout may occur after the server has accepted the submission.
    response = client.request("POST", "/api/submissions/submit", payload)
    submission_id = (response.get("data") or {}).get("submissionId") if isinstance(response, dict) else None
    if not submission_id:
        raise CannJudgeError("Submission response has no ID; check website records before retrying")
    object_id(submission_id)
    return {"submission_id": submission_id, "url": ORIGIN + "/submission/" + submission_id,
            "problem_id": problem_id, "user_id": payload["userId"], "file_count": len(files)}


def write_json(path, data):
    # Exclusive creation prevents an old reviewed plan or receipt from being silently replaced.
    try:
        with Path(path).open("x", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
    except OSError:
        raise CannJudgeError("Cannot create output file; choose a new path in an existing directory") from None


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise CannJudgeError("Cannot read JSON input file") from None


def status_summary(submission):
    # Operator scores come exclusively from the public ranking, not submission defaults.
    return {key: submission[key] for key in ("_id", "ID", "status", "valid", "create_time", "result")
            if key in submission}


def append_history(path, record):
    """Keep immutable observations; a later tbest update must not replace an old score."""
    try:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise CannJudgeError("Cannot append local history; choose a writable path in an existing directory") from None


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat()


def score_context(receipt=None, *, submission_id=None, problem_id=None, user_id=None):
    """Resolve explicit IDs without looking at cookies or private submission details."""
    if receipt is None:
        receipt = {}
    if not isinstance(receipt, dict):
        raise CannJudgeError("Expected a submission receipt object")
    context = {}
    for key, explicit in (("submission_id", submission_id), ("problem_id", problem_id), ("user_id", user_id)):
        saved = receipt.get(key)
        if explicit and saved and explicit.lower() != str(saved).lower():
            raise CannJudgeError(f"Explicit {key} conflicts with the receipt")
        value = explicit or saved
        if not value:
            raise CannJudgeError(f"Anonymous score lookup requires {key}; supply its flag or a receipt")
        context[key] = object_id(value).lower()
    return context


def ranking_entry(row):
    """Accept only the public ranking's actual score, rank and adopted submission."""
    submission = row.get("submission") or {}
    if not isinstance(submission, dict):
        raise CannJudgeError("Invalid ranking submission record")
    submission_id = object_id(row.get("submission_id")).lower()
    if submission.get("_id") and object_id(submission["_id"]).lower() != submission_id:
        raise CannJudgeError("Ranking submission IDs disagree")
    score, rank = row.get("score"), row.get("rank")
    if (isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score)
            or type(rank) is not int or rank < 1):
        raise CannJudgeError("Ranking has no valid numeric score and rank")
    number = submission.get("ID")
    if number is not None and (type(number) is not int or number < 1):
        raise CannJudgeError("Ranking has an invalid numeric submission ID")
    return {"submission_id": submission_id, "submission_number": number,
            "score": score, "rank": rank, "status": submission.get("status") or row.get("status")}


def score_snapshot(problem_id, user_id, submission_id):
    """Read an anonymous public ranking; never substitute a user's historical best."""
    context = score_context(submission_id=submission_id, problem_id=problem_id, user_id=user_id)
    path = f"/api/problems/{context['problem_id']}/ranking"
    snapshot = {"schema_version": 1, "record_type": "score", "queried_at_utc": utc_timestamp(),
                **context, "source": "public_problem_ranking", "source_url": ORIGIN + path,
                "cookie_used": False, "submission_score": None, "submission_rank": None,
                "leaderboard": None, "score_state": "not_ranked",
                "reason": "User has no entry in the public problem ranking"}
    try:
        client = Client(anonymous=True)
        page = 1
        while True:
            response = client.request("GET", f"{path}?page={page}&size=100")
            if (not isinstance(response, dict) or not isinstance(response.get("rows"), list)
                    or type(response.get("pages")) is not int or response["pages"] < 0
                    or type(response.get("current")) is not int or response["current"] != page):
                raise CannJudgeError("Unexpected public ranking pagination")
            matches = []
            for row in response["rows"]:
                if not isinstance(row, dict):
                    raise CannJudgeError("Invalid public ranking row")
                if str(row.get("user_id", "")).lower() == context["user_id"]:
                    matches.append(row)
            if len(matches) > 1:
                raise CannJudgeError("Public ranking contains duplicate user entries")
            if matches:
                entry = ranking_entry(matches[0])
                snapshot["leaderboard"] = entry
                if entry["submission_id"] == context["submission_id"]:
                    snapshot.update(submission_score=entry["score"], submission_rank=entry["rank"],
                                    score_state="matched", reason=None)
                else:
                    snapshot.update(score_state="ranking_uses_other_submission",
                                    reason="Public ranking adopts another submission; requested submission score/rank are not exposed")
                break
            if page >= response["pages"]:
                break
            if not response["rows"]:
                raise CannJudgeError("Public ranking returned an empty intermediate page")
            page += 1
    except CannJudgeError as error:
        snapshot.update(score_state="unavailable", reason=str(error))
    return snapshot


def save_history_or_report(result, history, record):
    # A disk/query failure after POST must not hide an accepted submission ID.
    try:
        append_history(history, record)
    except CannJudgeError as error:
        result["history_error"] = str(error)


def poll_submission(client, submission_id, wait):
    deadline = time.monotonic() + max(0, wait)
    while True:
        submission = client.submission(submission_id)
        if str(submission.get("status", "")).lower() not in {
                "waiting", "pending", "queued", "running", "judging", "compiling", "unjudged"}:
            return submission
        if time.monotonic() >= deadline:
            return submission
        time.sleep(min(3, max(0, deadline - time.monotonic())))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookie-file", type=Path, default=os.environ.get("CANNJUDGE_COOKIE_FILE"))
    parser.add_argument("--contest-url", default=DEFAULT_CONTEST)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("problems", help="List contest problems")
    files_parser = commands.add_parser("files", help="List template paths and editable flags")
    files_parser.add_argument("--problem", required=True)
    plan_parser = commands.add_parser("plan", help="Overlay local files and save a reviewable submission plan")
    plan_parser.add_argument("--problem", required=True)
    plan_parser.add_argument("--source", type=Path, required=True)
    plan_parser.add_argument("--file", action="append", help="Replace only these relative paths; repeat as needed")
    plan_parser.add_argument("--from-submission", help="Preserve other files from an earlier submission")
    plan_parser.add_argument("--diff", action="store_true", help="Include unified source diffs in the plan")
    plan_parser.add_argument("--output", type=Path, required=True)
    commit_parser = commands.add_parser("commit", help="Submit the exact files contained in a plan")
    commit_parser.add_argument("--plan", type=Path, required=True)
    commit_parser.add_argument("--wait", type=float, default=0, help="Optionally wait for judging before reading the ranking")
    commit_parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    status_parser = commands.add_parser("status", help="Query a submission, optionally polling")
    status_parser.add_argument("--id", required=True)
    status_parser.add_argument("--wait", type=float, default=0, help="Maximum polling duration in seconds")
    status_parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    score_parser = commands.add_parser("score", help="Read and save public ranking scores without cookies")
    score_parser.add_argument("--id", help="Requested submission's 24-character object ID")
    score_parser.add_argument("--problem-id")
    score_parser.add_argument("--user-id")
    score_parser.add_argument("--receipt", type=Path, help="JSON receipt containing submission_id, problem_id, user_id")
    score_parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    args = parser.parse_args(argv)
    client = Client(anonymous=True) if args.command == "score" else Client(args.cookie_file)
    if args.command == "problems":
        result = [{key: problem.get(key) for key in ("_id", "ID", "name", "title", "code_template")}
                  for problem in client.problems(args.contest_url)]
    elif args.command in {"files", "plan"}:
        problem = client.problem(args.contest_url, args.problem)
        template = client.template(problem["_id"])
        if args.command == "files":
            result = [{"path": file.path, "editable": file.editable} for file in template]
        else:
            previous = client.submission(args.from_submission) if args.from_submission else None
            plan = build_plan(problem, template, args.source, args.file, previous, args.diff)
            write_json(args.output, plan)
            result = {"plan": str(args.output), "problem": plan["problem_name"],
                      "file_count": len(plan["files"]), "changes": plan["changes"]}
    elif args.command == "commit":
        result = commit(client, read_json(args.plan))
        result["history"] = str(args.history)
        save_history_or_report(result, args.history, {
            "record_type": "submission_receipt", "saved_at_utc": utc_timestamp(), **result})
        if args.wait > 0:
            try:
                result["evaluation"] = status_summary(poll_submission(client, result["submission_id"], args.wait))
            except CannJudgeError as error:
                result["evaluation_error"] = str(error)
        result["score_snapshot"] = score_snapshot(**score_context(result))
        save_history_or_report(result, args.history, result["score_snapshot"])
    elif args.command == "score":
        receipt = read_json(args.receipt) if args.receipt else None
        context = score_context(receipt, submission_id=args.id, problem_id=args.problem_id, user_id=args.user_id)
        result = score_snapshot(**context)
        save_history_or_report(result, args.history, dict(result))
        result["history"] = str(args.history)
    else:
        submission = poll_submission(client, args.id, args.wait)
        result = status_summary(submission)
        result["history"] = str(args.history)
        try:
            context = score_context(submission_id=args.id, problem_id=submission.get("problem_id"),
                                    user_id=submission.get("user_id"))
            result["score_snapshot"] = score_snapshot(**context)
        except CannJudgeError as error:
            # Keep status usable if the server omits identity fields. Do not guess from a session.
            result["score_snapshot"] = {
                "schema_version": 1, "record_type": "score", "queried_at_utc": utc_timestamp(),
                "submission_id": args.id, "problem_id": submission.get("problem_id"),
                "user_id": submission.get("user_id"), "source": "public_problem_ranking",
                "source_url": None, "cookie_used": False, "submission_score": None,
                "submission_rank": None, "leaderboard": None, "score_state": "unavailable",
                "reason": str(error)}
        result["score_snapshot"].update(submission_number=submission.get("ID"),
                                        submission_status=submission.get("status"))
        save_history_or_report(result, args.history, result["score_snapshot"])
    print(json.dumps(client.printable(result), indent=2, ensure_ascii=False))
    return 1 if "history_error" in result else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CannJudgeError as error:
        print(f"cannjudge: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
