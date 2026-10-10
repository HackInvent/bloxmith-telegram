"""Short-lived Telegram Bot API exchange, run as an isolated child process.

Standard library only. The job, including the bot token, arrives on the private
stdin pipe, never in arguments or the environment. One JSON result is written
to stdout. The owning block kills this process group on cancellation, Stop or
deadline; a watchdog also exits if the parent disappears.
"""

import http.client
import json
import mimetypes
import os
import resource
import secrets
import socket
import ssl
import stat
import sys
import threading
import time
from urllib.parse import urlsplit

METHODS = {"getMe", "getUpdates", "sendMessage", "sendDocument", "sendPhoto"}
CHUNK = 65536


class Failure(Exception):
    """A content-free transport outcome reported to the parent."""

    def __init__(self, code, phase):
        super().__init__(code)
        self.code, self.phase = code, phase


def remaining(deadline, phase):
    left = deadline - time.monotonic()
    if left <= 0:
        raise Failure("timeout", phase)
    return max(0.001, left)


def open_inside(root, parts, limit):
    """Walk from the canonical root with O_NOFOLLOW so no symlink can be swapped in."""
    flags = os.O_RDONLY | os.O_CLOEXEC
    try:
        directory = os.open(root, flags | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Failure("file_not_allowed", "connect") from None
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        raise Failure("file_not_found", "connect") from None
    except OSError:
        raise Failure("file_not_allowed", "connect") from None
    finally:
        os.close(directory)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise Failure("file_not_allowed", "connect")
    if info.st_size > limit:
        os.close(descriptor)
        raise Failure("file_too_large", "connect")
    return descriptor, info


def quoted(name):
    """Header-safe multipart filename: no quotes, backslashes or control characters."""
    safe = "".join("_" if ord(char) < 32 or ord(char) == 127 or char in "\"\\" else char for char in name)
    return (safe or "upload")[:255]


def connection(job, deadline):
    origin = urlsplit(job["origin"])
    host, port = origin.hostname, origin.port
    if origin.scheme == "https":
        context = ssl.create_default_context(cafile=job["ca_file"] or None)
        return http.client.HTTPSConnection(host, port or 443, timeout=remaining(deadline, "connect"), context=context)
    return http.client.HTTPConnection(host, port or 80, timeout=remaining(deadline, "connect"))


def send_body(conn, job, path, deadline, opened):
    """Send one complete request. Until the last byte is written nothing can be processed."""
    upload = job.get("multipart")
    if upload is None:
        body = json.dumps(job.get("json") or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        conn.putrequest("POST", path, skip_host=False, skip_accept_encoding=True)
        for key, value in (("Content-Type", "application/json"), ("Content-Length", str(len(body)))):
            conn.putheader(key, value)
        parts = [body]
    else:
        descriptor, info = opened
        boundary = "BloxSmithTelegram" + secrets.token_hex(16)
        head = bytearray()
        for name, value in upload["fields"]:
            head += ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + name + "\"\r\n\r\n").encode("ascii")
            head += value.encode("utf-8") + b"\r\n"
        media = mimetypes.guess_type(upload["filename"])[0] or "application/octet-stream"
        head += ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" + upload["field"]
                 + "\"; filename=\"" + quoted(upload["filename"]) + "\"\r\nContent-Type: " + media
                 + "\r\n\r\n").encode("utf-8")
        tail = ("\r\n--" + boundary + "--\r\n").encode("ascii")
        conn.putrequest("POST", path, skip_host=False, skip_accept_encoding=True)
        conn.putheader("Content-Type", "multipart/form-data; boundary=" + boundary)
        conn.putheader("Content-Length", str(len(head) + info.st_size + len(tail)))
        parts = [bytes(head), (descriptor, info), tail]
    for key, value in (("Accept", "application/json"), ("Accept-Encoding", "identity"),
                       ("User-Agent", "BloxSmith-Telegram/0.1.0"), ("Connection", "close")):
        conn.putheader(key, value)
    conn.sock.settimeout(remaining(deadline, "send"))
    conn.endheaders()
    for part in parts:
        if isinstance(part, tuple):
            handle, info = part
            sent = 0
            while sent < info.st_size:
                chunk = os.read(handle, min(CHUNK, info.st_size - sent))
                if not chunk:
                    raise Failure("file_changed", "send")
                conn.sock.settimeout(remaining(deadline, "send"))
                conn.send(chunk)
                sent += len(chunk)
            after = os.fstat(handle)
            if os.read(handle, 1) or (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                raise Failure("file_changed", "send")
        else:
            conn.sock.settimeout(remaining(deadline, "send"))
            conn.send(part)


def exchange(job):
    """One request/response; no redirect, retry, cookie or decompression."""
    if job.get("method") not in METHODS:
        raise Failure("protocol", "connect")
    deadline = time.monotonic() + float(job["timeout_sec"])
    path = "/bot" + job["token"] + "/" + job["method"]
    phase = "connect"
    conn, opened = None, None
    try:
        upload = job.get("multipart")
        if upload is not None:
            # Validate the local file before any connection; failures send nothing.
            opened = open_inside(upload["root"], upload["parts"], upload["max_bytes"])
        conn = connection(job, deadline)
        conn.connect()
        phase = "send"
        send_body(conn, job, path, deadline, opened)
        phase = "receive"
        conn.sock.settimeout(remaining(deadline, phase))
        response = conn.getresponse()
        limit = int(job["max_response_bytes"])
        chunks, total = [], 0
        while True:
            conn_sock = conn.sock
            if conn_sock is not None:
                conn_sock.settimeout(remaining(deadline, phase))
            piece = response.read1(min(CHUNK, limit + 1 - total)) if total <= limit else b""
            if not piece:
                break
            total += len(piece)
            chunks.append(piece)
            if total > limit:
                break
        headers = {key.lower(): value for key, value in response.getheaders()}
        retry = headers.get("retry-after", "").strip()
        result = {"status": "response", "http_status": response.status,
                  "retry_after_header": int(retry) if retry.isdigit() and len(retry) <= 6 else None,
                  "body": None, "body_error": None}
        media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if total > limit:
            result["body_error"] = "too_large"
        elif headers.get("content-encoding", "identity").strip().lower() not in ("identity", ""):
            result["body_error"] = "encoded"
        elif media != "application/json":
            result["body_error"] = "not_json"
        else:
            try:
                result["body"] = json.loads(b"".join(chunks).decode("utf-8", errors="strict"),
                                            parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                result["body_error"] = "invalid_json"
        return result
    except Failure:
        raise
    except ssl.SSLCertVerificationError:
        raise Failure("tls", phase) from None
    except ssl.SSLError:
        raise Failure("tls" if phase == "connect" else "network", phase) from None
    except (socket.timeout, TimeoutError):
        raise Failure("timeout", phase) from None
    except (OSError, http.client.HTTPException, ValueError):
        raise Failure("network", phase) from None
    finally:
        if conn is not None:
            conn.close()
        if opened is not None:
            os.close(opened[0])


def reject_constant(_value):
    raise ValueError("Non-finite JSON number.")


def emit(value):
    data = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    view = memoryview(data)
    while view:
        written = os.write(1, view)
        view = view[written:]


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    parent = os.getppid()
    raw = sys.stdin.buffer.read(1048577)
    if len(raw) > 1048576:
        emit({"status": "error", "code": "protocol", "phase": "connect"})
        return 2
    try:
        job = json.loads(raw)
        budget = float(job["timeout_sec"])
    except (ValueError, KeyError, TypeError):
        emit({"status": "error", "code": "protocol", "phase": "connect"})
        return 2
    box = {}

    def target():
        try:
            box["result"] = exchange(job)
        except Failure as failure:
            box["result"] = {"status": "error", "code": failure.code, "phase": failure.phase}
        except Exception:
            box["result"] = {"status": "error", "code": "network", "phase": "receive"}

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    hard_deadline = time.monotonic() + budget + 2
    while worker.is_alive():
        worker.join(0.2)
        if os.getppid() != parent:
            os._exit(3)
        if time.monotonic() > hard_deadline:
            emit({"status": "error", "code": "timeout", "phase": "receive"})
            os._exit(4)
    emit(box["result"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
