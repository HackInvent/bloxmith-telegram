"""Own each Telegram exchange in a killable child process and interpret its outcome.

Credentials travel only through the child's private stdin pipe. Cancellation,
Stop and deadlines terminate the whole child process group. Only HTTP 429 is
retried, because Telegram did not process that request; ambiguous failures
are reported, never replayed.
"""

import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from . import logic

WORKER = Path(__file__).with_name("telegram_worker.py")
ENVIRONMENT = {"PATH": os.defpath, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
OUTPUT_LIMIT = 3 * 8388608


class WorkerCall:
    """One running exchange. poll() is non-blocking enough for Stop-aware loops."""

    def __init__(self, job):
        self.payload = json.dumps(job, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self.child = subprocess.Popen([sys.executable, "-I", "-B", "-u", str(WORKER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            env=dict(ENVIRONMENT))
        self.first, self.output = True, None

    def poll(self, timeout=0.05):
        """Return the worker result once finished, else None after at most timeout seconds."""
        if self.output is not None:
            return self.output
        try:
            output, _ = self.child.communicate(input=self.payload if self.first else None, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.first = False
            return None
        self.first = False
        if len(output) > OUTPUT_LIMIT:
            self.output = {"status": "error", "code": "protocol", "phase": "receive"}
        else:
            try:
                value = json.loads(output)
                self.output = value if isinstance(value, dict) else {"status": "error", "code": "protocol", "phase": "receive"}
            except (ValueError, UnicodeError):
                self.output = {"status": "error", "code": "protocol", "phase": "receive"}
        self.close()
        return self.output

    def close(self):
        """Terminate and reap only this process group; idempotent."""
        child = self.child
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=2)
        for stream in (child.stdin, child.stdout):
            if stream and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass


def job_for(config, token, method, *, json_params=None, upload=None, timeout_sec):
    if method not in logic.METHODS:
        logic.fail("internal")
    return {"origin": config["api_base_url"], "token": token, "method": method, "ca_file": config["ca_file"],
            "timeout_sec": float(timeout_sec), "max_response_bytes": config["max_response_bytes"],
            "json": json_params, "multipart": upload}


def interpret(outcome, token, *, mutation):
    """Map a worker outcome to the Bot API result or a structured, token-free error."""
    if outcome.get("status") == "response":
        if outcome.get("body_error") is not None:
            status = outcome.get("http_status")
            if type(status) is int and status >= 500:
                logic.fail("provider_unavailable", uncertain=mutation, http_status=status)
            if type(status) is int and status in (401, 404, 429):
                logic.fail(logic.map_status(status), http_status=status,
                           retry_after=outcome.get("retry_after_header") if status == 429 else None)
            logic.fail("protocol", uncertain=mutation and type(status) is int and status < 300,
                       http_status=status if type(status) is int else None)
        try:
            return logic.provider_result(outcome, token)
        except logic.TelegramError as exc:
            if not mutation:
                exc.uncertain = False
            raise
    code, phase = outcome.get("code"), outcome.get("phase")
    after_send = phase == "receive"
    if code in ("file_not_found", "file_not_allowed", "file_too_large", "file_changed", "tls"):
        logic.fail(code)
    if code == "timeout":
        logic.fail("timeout", logic.MESSAGES["timeout_uncertain" if after_send and mutation else "timeout"],
                   uncertain=after_send and mutation)
    if code == "network":
        logic.fail("network", logic.MESSAGES["network_uncertain" if after_send and mutation else "network"],
                   uncertain=after_send and mutation)
    logic.fail("protocol", uncertain=after_send and mutation)


def call(config, token, method, *, json_params=None, upload=None, deadline, cancel=lambda: False,
         read_timeout=None):
    """Run one exchange while honouring the caller deadline and cancellation."""
    mutation = method.startswith("send")
    budget = deadline - time.monotonic()
    if read_timeout is not None:
        budget = min(budget, read_timeout)
    if budget <= 0.5:
        logic.fail("timeout")
    if cancel():
        logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
    worker = WorkerCall(job_for(config, token, method, json_params=json_params, upload=upload, timeout_sec=budget))
    try:
        while True:
            outcome = worker.poll(0.05)
            if outcome is not None:
                return interpret(outcome, token, mutation=mutation)
            if cancel():
                logic.fail("cancelled", uncertain=mutation)
            if time.monotonic() >= deadline + 1:
                logic.fail("timeout", logic.MESSAGES["timeout_uncertain" if mutation else "timeout"], uncertain=mutation)
    finally:
        worker.close()


def pause(seconds, cancel):
    """Cancellation-aware wait used only between a 429 response and its retry."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cancel():
            logic.fail("cancelled", logic.MESSAGES["cancelled_before_send"])
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


def send(config, token, request, cancel=lambda: False, located=None):
    """Send one message or file, retrying only after HTTP 429 within the documented bounds."""
    deadline = time.monotonic() + config["timeout_sec"]
    method, upload = request["method"], None
    if request["operation"] == "send_file":
        method, field = logic.file_method(request, config, located["size"], located["suffix"])
        upload = {"root": located["root"], "parts": located["parts"], "field": field,
                  "filename": request["filename"] or located["name"], "max_bytes": min(
                      config["max_file_bytes"], logic.PHOTO_LIMIT if field == "photo" else config["max_file_bytes"]),
                  "fields": logic.multipart_fields(request["params"])}
    attempts = 0
    while True:
        attempts += 1
        try:
            if upload is None:
                result = call(config, token, method, json_params=request["params"], deadline=deadline, cancel=cancel)
            else:
                result = call(config, token, method, upload=upload, deadline=deadline, cancel=cancel)
            return method, logic.normalize_sent(result, request, method, attempts)
        except logic.TelegramError as exc:
            wait = exc.retry_after if exc.retry_after is not None else 1
            if (exc.code == "rate_limited" and attempts <= config["max_retries"]
                    and wait <= config["max_retry_wait_sec"] and time.monotonic() + wait + 1 < deadline):
                pause(wait, cancel)
                continue
            exc.attempts = attempts
            raise


def locate(path_text, config):
    """Resolve an input path strictly inside file_root, refusing links and special files."""
    if not config["file_root"]:
        logic.fail("file_root_required")
    configured = Path(os.path.normpath(config["file_root"]))
    try:
        root = configured.resolve(strict=True)
    except (OSError, RuntimeError):
        logic.fail("file_root_required", "file_root does not exist or cannot be read.")
    if not root.is_dir() or root == Path(root.anchor):
        logic.fail("file_root_required", "file_root must be an existing dedicated directory, not a filesystem root.")
    candidate = Path(path_text)
    if any(part in ("..", ".") for part in candidate.parts):
        logic.fail("file_not_allowed")
    if candidate.is_absolute():
        normalized = Path(os.path.normpath(candidate))
        for base in (configured, root):
            try:
                relative = normalized.relative_to(base)
                break
            except ValueError:
                continue
        else:
            logic.fail("file_not_allowed")
    else:
        relative = candidate
    parts = [part for part in relative.parts if part not in ("", "/")]
    if not parts:
        logic.fail("file_not_allowed")
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            logic.fail("file_not_found")
        except OSError:
            logic.fail("file_not_allowed")
        last = index == len(parts) - 1
        if stat.S_ISLNK(info.st_mode) or (not last and not stat.S_ISDIR(info.st_mode)):
            logic.fail("file_not_allowed")
        if last and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            logic.fail("file_not_allowed")
    if info.st_size > config["max_file_bytes"]:
        logic.fail("file_too_large")
    return {"root": str(root), "parts": parts, "size": info.st_size, "suffix": current.suffix,
            "name": current.name}
