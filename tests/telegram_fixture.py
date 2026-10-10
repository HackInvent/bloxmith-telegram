"""Loopback-only Telegram Bot API double. No real bot, token, chat or provider is contacted.

It reproduces the documented request/response shapes used by the block:
getMe, getUpdates (offset confirmation, limit, long-poll timeout), sendMessage,
sendDocument and sendPhoto (multipart), and the {"ok": false, "error_code",
"description", "parameters"} error envelope, including retry_after.
"""

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

TOKEN = "424242:fixture-telegram-token-not-a-real-one"
BOT = {"id": 424242, "is_bot": True, "first_name": "Fixture", "username": "fixture_bot"}
ALLOWED, OTHER, GROUP = 1001, 2002, -1003003003
REF = "secret://workspace/telegram_fixture"


def settings(api, **extra):
    """Block settings aimed at this fixture only."""
    return {"enabled": True, "secret_ref": REF, "allowed_chat_ids": [ALLOWED, GROUP], "default_chat_id": str(ALLOWED),
            "api_base_url": api.base_url, "ca_file": api.ca_file, "poll_timeout_sec": 2, "timeout_sec": 15, **extra}


def message(update_id, chat_id, text, *, kind="message", sender=7, extra=None):
    """A documented Update object carrying one Message."""
    body = {"message_id": 500 + update_id, "date": 1760000000 + update_id,
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup", "first_name": "Ada"},
            "from": {"id": sender, "is_bot": False, "first_name": "Ada", "username": "ada", "language_code": "fr"},
            "text": text}
    body.update(extra or {})
    return {"update_id": update_id, kind: body}


def parse_multipart(body, content_type):
    """Strict small multipart reader for the fixture; returns (fields, files)."""
    match = re.search(r'boundary=([A-Za-z0-9_-]+)', content_type)
    assert match, content_type
    delimiter = b"--" + match.group(1).encode("ascii")
    sections = body.split(delimiter)
    assert sections[0] == b"" and sections[-1] == b"--\r\n", "malformed multipart framing"
    fields, files = {}, {}
    for section in sections[1:-1]:
        assert section.startswith(b"\r\n") and section.endswith(b"\r\n")
        head, separator, content = section[2:-2].partition(b"\r\n\r\n")
        assert separator
        headers = {}
        for line in head.decode("utf-8").split("\r\n"):
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
        disposition = headers["content-disposition"]
        name = re.search(r'(?:^|;)\s*name="([^"]*)"', disposition).group(1)
        filename = re.search(r'filename="([^"]*)"', disposition)
        if filename:
            files[name] = {"filename": filename.group(1), "size": len(content), "content_type": headers.get("content-type"),
                           "sha256": hashlib.sha256(content).hexdigest()}
        else:
            fields[name] = content.decode("utf-8")
    return fields, files


@contextmanager
def telegram_api(tls=False):
    """Run the double on a dynamic loopback port; every owned thread is joined on exit."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        changed = threading.Condition()
        api = SimpleNamespace(calls=[], updates=[], script={}, sent=[], next_message=1, release=threading.Event(),
                              closing=threading.Event(), active_polls=0, max_poll_wait=3.0)

        def queue(*items):
            with changed:
                api.updates.extend(items)
                changed.notify_all()
        api.queue = queue

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def reply(self, status, body, *, raw=None, content_type="application/json", headers=None):
                data = raw if raw is not None else json.dumps(body).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    for key, value in (headers or {}).items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionError, ssl.SSLError):
                    pass
                finally:
                    self.close_connection = True

            def do_GET(self):
                self.reply(405, {"ok": False, "error_code": 405, "description": "Method Not Allowed"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                assert 0 <= length <= 60 * 1024 * 1024
                body = self.rfile.read(length)
                match = re.fullmatch(r"/bot([^/]+)/([A-Za-z]+)", self.path)
                if not match:
                    self.reply(404, {"ok": False, "error_code": 404, "description": "Not Found"})
                    return
                token, method = match.groups()
                content_type = self.headers.get("Content-Type", "")
                if content_type.startswith("multipart/form-data"):
                    params, files = parse_multipart(body, content_type)
                else:
                    assert content_type == "application/json", content_type
                    params, files = json.loads(body or b"{}"), {}
                call = {"method": method, "params": params, "files": files, "token_ok": token == TOKEN,
                        "accept_encoding": self.headers.get("Accept-Encoding"), "at": time.monotonic()}
                api.calls.append(call)
                if token != TOKEN:
                    self.reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
                    return
                scripted = api.script.get(method)
                if scripted:
                    action = scripted.pop(0)
                    if action == "hold":
                        api.release.wait(20)
                    elif action == "lost":
                        try:
                            self.connection.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        self.close_connection = True
                        return
                    elif action == "html502":
                        self.reply(502, None, raw=b"<html>Bad Gateway</html>", content_type="text/html")
                        return
                    elif action == "oversized":
                        self.reply(200, {"ok": True, "result": {"padding": "x" * 200000}})
                        return
                    elif isinstance(action, dict):
                        self.reply(action["status"], action["body"], headers=action.get("headers"))
                        return
                if method == "getMe":
                    self.reply(200, {"ok": True, "result": BOT})
                elif method == "getUpdates":
                    self.get_updates(params)
                elif method == "sendMessage":
                    self.send_result(params, {"text": params["text"]})
                elif method in ("sendDocument", "sendPhoto"):
                    field = "document" if method == "sendDocument" else "photo"
                    upload = files[field]
                    descriptor = {"file_id": "fixture-file-" + upload["sha256"][:12], "file_unique_id": upload["sha256"][:8],
                                  "file_size": upload["size"]}
                    if field == "document":
                        content = {"document": {**descriptor, "file_name": upload["filename"],
                                                "mime_type": upload["content_type"]}}
                    else:
                        content = {"photo": [{**descriptor, "width": 90, "height": 90},
                                             {**descriptor, "file_id": descriptor["file_id"] + "-big", "width": 800, "height": 600}]}
                    if params.get("caption"):
                        content["caption"] = params["caption"]
                    self.send_result(params, content)
                else:
                    self.reply(404, {"ok": False, "error_code": 404, "description": "Not Found: method not found"})

            def send_result(self, params, content):
                chat = int(params["chat_id"])
                if chat == OTHER:
                    self.reply(400, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})
                    return
                with changed:
                    ident = api.next_message
                    api.next_message += 1
                result = {"message_id": ident, "date": 1760000000, "chat": {"id": chat, "type": "private" if chat > 0 else "supergroup"},
                          **content}
                if "message_thread_id" in params:
                    result["message_thread_id"] = int(params["message_thread_id"])
                api.sent.append(result)
                self.reply(200, {"ok": True, "result": result})

            def get_updates(self, params):
                offset = params.get("offset")
                limit = int(params.get("limit", 100))
                wait = min(float(params.get("timeout", 0)), api.max_poll_wait)
                deadline = time.monotonic() + wait
                with changed:
                    api.active_polls += 1
                    try:
                        if offset is not None:
                            # Calling getUpdates with a higher offset confirms earlier updates.
                            api.updates[:] = [item for item in api.updates if item["update_id"] >= int(offset)]
                        while not api.updates and not api.closing.is_set():
                            left = deadline - time.monotonic()
                            if left <= 0:
                                break
                            changed.wait(min(left, 0.1))
                        selected = [item for item in api.updates if offset is None or item["update_id"] >= int(offset)][:limit]
                    finally:
                        api.active_polls -= 1
                if not api.closing.is_set():
                    self.reply(200, {"ok": True, "result": selected})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        ca = ""
        if tls:
            assert shutil.which("openssl"), "OpenSSL is required for the disposable HTTPS fixture"
            certificate, key = directory / "ca.pem", directory / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(key), "-out", str(certificate), "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"], check=True, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            ca = str(certificate)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        api.base_url = ("https" if tls else "http") + "://127.0.0.1:" + str(server.server_port)
        api.ca_file = ca
        try:
            yield api
        finally:
            api.closing.set()
            api.release.set()
            with changed:
                changed.notify_all()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()
