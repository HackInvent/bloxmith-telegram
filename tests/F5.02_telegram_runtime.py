"""FB2/FB3/FB4/FB5: loopback Bot API protocol, long polling, Stop, and both runtimes in every package origin.

Every provider exchange targets the loopback double in telegram_fixture.py. The
wallet and its fixture token exist only in disposable test storage.
"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.telegram import listener, logic, storage, transport
from blocs.telegram.block import TelegramBlock
from block_test_packages import install_test_package, prepare_release_run, surface_payload
from telegram_fixture import ALLOWED, BOT, GROUP, OTHER, REF, TOKEN, message, settings, telegram_api
from ui_smoke_common import (create_project_api, create_run_api, data_edge, display_node, graph_payload, graph_storage_dir,
                             http_json, isolated_server, stop_run_api, text_node, wait_for_run_predicate, wait_for_run_terminal)


def until(predicate, timeout=10.0, message_text="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(message_text)


def failure(call, code):
    try:
        call()
    except logic.TelegramError as exc:
        assert exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in json.dumps(exc.envelope())
        return exc
    raise AssertionError("Expected " + code)


class Tracking(transport.WorkerCall):
    """Record every owned network child so tests can prove it was reaped."""
    created = []

    def __init__(self, job):
        super().__init__(job)
        Tracking.created.append(self)


def send_protocol():
    """# - FB2 - # - FB3 - sendMessage/sendDocument/sendPhoto, coded errors, 429 retries and cancellation."""
    with telegram_api() as api, tempfile.TemporaryDirectory() as folder:
        share = Path(folder) / "share"
        share.mkdir()
        (share / "report.pdf").write_bytes(b"%PDF-1.4\n" + bytes(range(256)) * 40)
        (share / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048)
        config = logic.configuration(settings(api, file_root=str(share), allowed_chat_ids=[ALLOWED, GROUP, OTHER],
                                              timeout_sec=10, max_retry_wait_sec=2))
        method, sent = transport.send(config, TOKEN, logic.message_request(
            {"text": "Héllo <b>team</b>", "parse_mode": "HTML", "request_id": "r-1", "disable_link_preview": True}, config))
        call = api.calls[-1]
        assert method == "sendMessage" and sent["chat_id"] == ALLOWED and sent["request_id"] == "r-1" and sent["attempts"] == 1
        assert call["params"] == {"chat_id": ALLOWED, "text": "Héllo <b>team</b>", "parse_mode": "HTML",
                                  "link_preview_options": {"is_disabled": True}} and call["accept_encoding"] == "identity"
        request = logic.file_request({"path": "report.pdf", "caption": "Q3 report", "chat_id": GROUP}, config)
        located = transport.locate(request["path"], config)
        method, sent = transport.send(config, TOKEN, request, located=located)
        upload = api.calls[-1]["files"]["document"]
        assert method == "sendDocument" and sent["operation"] == "send_document" and sent["file"]["file_name"] == "report.pdf"
        assert upload["sha256"] == hashlib.sha256((share / "report.pdf").read_bytes()).hexdigest()
        assert upload["filename"] == "report.pdf" and api.calls[-1]["params"] == {"chat_id": str(GROUP), "caption": "Q3 report"}
        request = logic.file_request({"path": "chart.png", "filename": "Q3 – chart.png"}, config)
        method, sent = transport.send(config, TOKEN, request, located=transport.locate("chart.png", config))
        assert method == "sendPhoto" and sent["file"]["file_id"].endswith("-big") and api.calls[-1]["files"]["photo"]["filename"] == "Q3 – chart.png"
        text = logic.message_request("ping", config)
        failure(lambda: transport.send(config, TOKEN, logic.message_request({"text": "x", "chat_id": OTHER}, config)), "bad_request")
        failure(lambda: transport.send(config, "1:" + "x" * 30, text), "unauthorized")
        envelope = {"ok": False, "error_code": 429, "description": "Too Many Requests: retry after 1", "parameters": {"retry_after": 1}}
        api.script["sendMessage"] = [{"status": 429, "body": envelope}]
        started, before = time.monotonic(), len(api.calls)
        assert transport.send(config, TOKEN, text)[1]["attempts"] == 2 and time.monotonic() - started >= 1
        assert len(api.calls) == before + 2
        api.script["sendMessage"] = [{"status": 429, "body": {**envelope, "parameters": {"retry_after": 30}}}]
        limited = failure(lambda: transport.send(config, TOKEN, text), "rate_limited")
        assert limited.retry_after == 30 and limited.attempts == 1 and not limited.uncertain
        api.script["sendMessage"] = [{"status": 429, "body": envelope}] * 3
        exhausted = failure(lambda: transport.send(logic.configuration({**settings(api), "max_retries": 0}), TOKEN, text), "rate_limited")
        assert exhausted.attempts == 1
        api.script["sendMessage"] = []
        for scripted, code, uncertain in (({"status": 403, "body": {"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}}, "forbidden", False),
                                          ({"status": 404, "body": {"ok": False, "error_code": 404, "description": "Not Found"}}, "not_found", False),
                                          ({"status": 500, "body": {"ok": False, "error_code": 500, "description": "Internal Server Error"}}, "provider_unavailable", True),
                                          ("html502", "provider_unavailable", True), ("oversized", "protocol", True), ("lost", "network", True)):
            api.script["sendMessage"] = [scripted]
            before = len(api.calls)
            error = failure(lambda: transport.send(logic.configuration({**settings(api), "max_response_bytes": 65536}), TOKEN, text), code)
            assert error.uncertain is uncertain and len(api.calls) == before + 1, (scripted, error.uncertain)
        Tracking.created.clear()
        with patch.object(transport, "WorkerCall", Tracking):
            api.script["sendMessage"] = ["hold"]
            started = time.monotonic()
            cancelled = failure(lambda: transport.send(config, TOKEN, text, cancel=lambda: time.monotonic() - started > 0.4), "cancelled")
            assert cancelled.uncertain and time.monotonic() - started < 2.5
            api.release.set()
            api.script["sendMessage"] = ["hold"]
            api.release.clear()
            started = time.monotonic()
            timed = failure(lambda: transport.send(logic.configuration({**settings(api), "timeout_sec": 5}), TOKEN, text), "timeout")
            assert timed.uncertain and 4 <= time.monotonic() - started < 8
            api.release.set()
        assert Tracking.created and all(item.child.poll() is not None for item in Tracking.created)
        closed = logic.configuration({**settings(api), "api_base_url": "http://127.0.0.1:9"})
        assert not failure(lambda: transport.send(closed, TOKEN, text), "network").uncertain
    with telegram_api(tls=True) as api:
        config = logic.configuration(settings(api))
        assert transport.send(config, TOKEN, logic.message_request("over TLS", config))[1]["message_id"] == 1
        before = len(api.calls)
        untrusted = failure(lambda: transport.send(logic.configuration({**settings(api), "ca_file": ""}), TOKEN,
                                                   logic.message_request("x", config)), "tls")
        assert not untrusted.uncertain and len(api.calls) == before
    print("[ok] Telegram send protocol, files, errors, 429 retries, cancellation and TLS", flush=True)


def poller(config, directory, emitted, stop, token=TOKEN):
    def emit(kind, value, stats):
        if stop.is_set():
            raise RuntimeError("stopped")
        emitted.append((kind, value, stats))
    instance = listener.UpdatePoller(config, resolve_token=lambda: token, storage_dir=lambda: directory, emit=emit,
                                     stop=stop.is_set)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    return thread


def finish(stop, thread):
    started = time.monotonic()
    stop.set()
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - started < 1.5, "Listener did not stop promptly"


def receive_protocol():
    """# - FB4 - paged long polling, allowlist drops, persisted offsets, restart without replay and prompt Stop."""
    quick = lambda failures, code, retry_after=None: 0.2
    with telegram_api() as api, tempfile.TemporaryDirectory() as folder, patch.object(logic, "backoff_delay", quick), \
            patch.object(transport, "WorkerCall", Tracking):
        directory = Path(folder)
        config = logic.configuration(settings(api, receive_updates=True, poll_limit=2, poll_timeout_sec=1,
                                              max_updates_per_minute=120))
        api.queue(*[message(index, ALLOWED, "note " + str(index)) for index in range(1, 6)],
                  message(6, OTHER, "stranger"), {"update_id": 7, "callback_query": {"id": "q"}})
        emitted, stop = [], threading.Event()
        thread = poller(config, directory, emitted, stop)
        received = lambda: [value["update_id"] for kind, value, _stats in emitted if kind == "received"]
        until(lambda: received() == [1, 2, 3, 4, 5], 10, "updates were not delivered in order")
        until(lambda: any(kind == "status" and stats["dropped_chat"] == 1 and stats["dropped_type"] == 1
                          for kind, _value, stats in emitted), 10, "dropped counters missing")
        polls = [call["params"] for call in api.calls if call["method"] == "getUpdates"]
        assert polls[0] == {"limit": 2, "timeout": 1, "allowed_updates": ["message"]}
        assert [item.get("offset") for item in polls[:5]] == [None, 3, 5, 7, 8], polls
        assert all(item["limit"] == 2 for item in polls)
        Tracking.created.clear()
        finish(stop, thread)
        assert all(item.child.poll() is not None for item in Tracking.created)
        state = json.loads((directory / storage.STATE_NAME).read_text())
        assert state["bots"][str(BOT["id"])]["offset"] == 8 and TOKEN not in json.dumps(state)
        # A new Run resumes after the stored offset: nothing already handed off is replayed.
        api.queue(message(8, ALLOWED, "after restart"))
        emitted, stop = [], threading.Event()
        before = len(api.calls)
        thread = poller(config, directory, emitted, stop)
        until(lambda: received() == [8], 10, "restart did not deliver the new update")
        first = next(call for call in api.calls[before:] if call["method"] == "getUpdates")
        assert first["params"]["offset"] == 8
        finish(stop, thread)
        # Stored offsets older than Telegram's 24 h retention are not trusted.
        stale = storage.record({"schema_version": 1, "bots": {}}, BOT["id"], 999, int(time.time()) - 90000)
        storage.OffsetStore(directory).save(stale)
        emitted, stop = [], threading.Event()
        before = len(api.calls)
        thread = poller(config, directory, emitted, stop)
        until(lambda: any(call["method"] == "getUpdates" for call in api.calls[before:]), 10)
        assert "offset" not in next(call for call in api.calls[before:] if call["method"] == "getUpdates")["params"]
        finish(stop, thread)
        # Provider failures are reported once per distinct code, then polling recovers.
        api.script["getUpdates"] = [{"status": 409, "body": {"ok": False, "error_code": 409,
            "description": "Conflict: terminated by other getUpdates request"}},
            {"status": 429, "body": {"ok": False, "error_code": 429, "description": "Too Many Requests",
                                     "parameters": {"retry_after": 1}}}]
        api.queue(message(9, ALLOWED, "after errors"))
        emitted, stop = [], threading.Event()
        thread = poller(config, directory, emitted, stop)
        until(lambda: 9 in received(), 15, "polling did not recover after 409/429")
        errors = [value for kind, value, _stats in emitted if kind == "error"]
        assert [item["code"] for item in errors] == ["conflict", "rate_limited"] and errors[1]["retry_after"] == 1
        assert all(item["source"] == "receive" and item["operation"] == "get_updates" for item in errors)
        finish(stop, thread)
        # Stop during a held long poll terminates the owned child immediately.
        api.max_poll_wait = 30
        Tracking.created.clear()
        emitted, stop = [], threading.Event()
        thread = poller(logic.configuration({**settings(api), "receive_updates": True, "poll_timeout_sec": 30}), directory, emitted, stop)
        until(lambda: api.active_polls >= 1, 10, "long poll not started")
        time.sleep(0.3)
        finish(stop, thread)
        assert Tracking.created and all(item.child.poll() is not None for item in Tracking.created)
        api.max_poll_wait = 3.0
        # A refused hand-off restores the stored offset so the next Run receives that update again.
        api.queue(message(10, ALLOWED, "accepted"), message(11, ALLOWED, "refused"))
        emitted, stop, refused = [], threading.Event(), []

        def flaky(kind, value, stats):
            if kind == "received" and value["update_id"] == 11:
                refused.append(value)
                raise RuntimeError("runtime_listeners.queue_full")
            emitted.append((kind, value, stats))
        instance = listener.UpdatePoller(config, resolve_token=lambda: TOKEN, storage_dir=lambda: directory,
                                         emit=flaky, stop=stop.is_set)
        thread = threading.Thread(target=instance.run, daemon=True)
        thread.start()
        until(lambda: refused, 10, "refused hand-off not attempted")
        time.sleep(0.3)
        assert json.loads((directory / storage.STATE_NAME).read_text())["bots"][str(BOT["id"])]["offset"] == 11
        assert [item for item in api.calls if item["method"] == "getUpdates"][-1]["params"]["offset"] == 10
        finish(stop, thread)
        # Wrong token, missing storage, malformed state and a held lock all fail closed with one error.
        emitted, stop = [], threading.Event()
        thread = poller(config, directory, emitted, stop, token="1:" + "y" * 30)
        until(lambda: any(kind == "error" and value["code"] == "unauthorized" for kind, value, _ in emitted), 10)
        finish(stop, thread)
        emitted, stop = [], threading.Event()
        thread = poller(config, directory, emitted, stop, token="not a token")
        until(lambda: any(kind == "error" and value["code"] == "secret" for kind, value, _ in emitted), 10)
        finish(stop, thread)
        (directory / storage.STATE_NAME).write_text("{broken")
        before = len(api.calls)
        emitted, stop = [], threading.Event()
        thread = poller(config, directory, emitted, stop)
        until(lambda: any(kind == "error" and value["code"] == "state_invalid" for kind, value, _ in emitted), 10)
        time.sleep(0.3)
        assert len(api.calls) == before, "A malformed offset must stop polling before any request"
        finish(stop, thread)
        (directory / storage.STATE_NAME).unlink()
        holder = storage.OffsetStore(directory)
        assert holder.acquire()
        emitted, stop = [], threading.Event()
        thread = poller(config, directory, emitted, stop)
        until(lambda: any(kind == "error" and value["code"] == "busy" for kind, value, _ in emitted), 10)
        assert len(api.calls) == before
        holder.close()
        until(lambda: any(kind == "status" and stats["state"] == "listening" for kind, _value, stats in emitted), 10)
        finish(stop, thread)
        emitted, stop = [], threading.Event()
        missing = listener.UpdatePoller(config, resolve_token=lambda: TOKEN, storage_dir=lambda: (_ for _ in ()).throw(RuntimeError("block_storage.missing_scope")),
                                        emit=lambda kind, value, stats: emitted.append((kind, value, stats)), stop=stop.is_set)
        thread = threading.Thread(target=missing.run, daemon=True)
        thread.start()
        until(lambda: any(kind == "error" and value["code"] == "storage" for kind, value, _ in emitted), 10)
        finish(stop, thread)
    print("[ok] Telegram long polling, offsets, drops, recovery, refused hand-off and Stop", flush=True)


def value(data, port):
    raw = data.get("output_values", {}).get("bot:" + str(port), {}).get("value")
    return json.loads(raw) if raw else {}


def stats(data):
    return (((data.get("results") or {}).get("bot") or {}).get("telegram") or {}).get("receive") or {}


def wallet(server):
    http_json(server.base_url, "/api/application/secrets/init", method="POST", payload={"password": "disposable-telegram-fixture"})
    http_json(server.base_url, "/api/application/secrets", method="POST", payload={"name": "telegram_fixture", "value": TOKEN})


def graph_runs():
    """# - FB2 - # - FB4 - # - FB5 - real graphs in both runtimes for bundled, managed and linked packages."""
    block = TelegramBlock()
    for mode in ("centralized", "zeromq_active"):
        for origin in (None, "managed", "linked"):
            full = origin is None
            with telegram_api() as api, isolated_server() as server:
                share = server.root_dir / "share"
                share.mkdir()
                (share / "invoice.pdf").write_bytes(b"%PDF-1.4 fixture invoice")
                node = block.build_node_payload(node_id="bot", position={"x": 380, "y": 160}, config_overrides=settings(
                    api, receive_updates=True, file_root=str(share), poll_timeout_sec=1, max_updates_per_minute=120))
                if origin:
                    model = install_test_package(server, "telegram", origin=origin)
                    node["block_version"] = model["version"]
                    for surface in ("modal", "inspector_panel", "node_card"):
                        html = surface_payload(server, model, node, surface=surface)["html"]
                        assert "{{" not in html and TOKEN not in html
                        if surface != "node_card":
                            assert 'data-block-config-field="allowed_chat_ids"' in html
                wallet(server)
                sinks = [display_node("inbox", "Inbox", 760, 40), display_node("receipt", "Receipt", 760, 200),
                         display_node("problem", "Problem", 760, 360)]
                edges = [data_edge("rx", "bot", 1, "inbox", 1), data_edge("tx", "bot", 2, "receipt", 1),
                         data_edge("err", "bot", 3, "problem", 1)]
                if mode == "centralized":
                    def run(port, text):
                        graph = graph_payload("Telegram one shot", [text_node("source", "Source", text, 60, 160), node, *sinks],
                                              [data_edge("in", "source", 1, "bot", port), *edges])
                        project = create_project_api(server, document=graph)["project"]
                        created = create_run_api(server, graph, project_id=project["graph_id"], runtime_mode=mode)
                        data = wait_for_run_terminal(server, created["run_id"], timeout_sec=40)
                        assert data["status"] == "success", (origin, data.get("logs"))
                        assert TOKEN not in json.dumps(data)
                        return data
                    sent = value(run(1, "Build finished"), 2)
                    assert sent["chat_id"] == ALLOWED and sent["operation"] == "send_message" and api.sent[-1]["text"] == "Build finished"
                    assert not [call for call in api.calls if call["method"] in ("getUpdates", "getMe")], "One Shot must never poll"
                    if full:
                        document = value(run(2, json.dumps({"path": "invoice.pdf", "caption": "Invoice"})), 2)
                        assert document["operation"] == "send_document" and api.calls[-1]["files"]["document"]["size"] == 24
                        before = len(api.calls)
                        refused = value(run(1, json.dumps({"text": "leak", "chat_id": OTHER})), 3)
                        assert refused["code"] == "chat_not_allowed" and len(api.calls) == before
                        outside = value(run(2, str(server.root_dir / "user")), 3)
                        assert outside["code"] in ("file_not_allowed", "file_not_found") and len(api.calls) == before
                        http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                        locked = value(run(1, "wallet locked"), 3)
                        assert locked["code"] == "secret" and len(api.calls) == before
                else:
                    graph = graph_payload("Telegram bot", [text_node("cmd", "Command", "", 60, 160), node, *sinks],
                                          [data_edge("in", "cmd", 1, "bot", 1), *edges])
                    project = create_project_api(server, document=graph)["project"]
                    state_file = graph_storage_dir(server, project) / "instances" / "1" / "blocs" / "bot" / storage.STATE_NAME
                    api.queue(message(1, ALLOWED, "/start@fixture_bot hello"), message(2, OTHER, "spam"))

                    def start():
                        rid = prepare_release_run(server, project["graph_id"], graph)["run_id"]
                        http_json(server.base_url, f"/api/runs/{rid}/play", method="POST", payload={})
                        return rid

                    def wait(rid, predicate, label):
                        data = wait_for_run_predicate(server, rid, lambda item: item.get("status") == "failed" or predicate(item),
                                                      label, timeout_sec=30)
                        assert data.get("status") != "failed", data.get("logs")
                        assert TOKEN not in json.dumps(data)
                        return data

                    def stop(rid):
                        started = time.monotonic()
                        stop_run_api(server, rid)
                        stopped = wait_for_run_terminal(server, rid, timeout_sec=20)
                        assert time.monotonic() - started < 10, stopped.get("logs")
                        assert "shutdown_timeout" not in json.dumps(stopped.get("logs"))
                        return stopped

                    rid = start()
                    try:
                        data = wait(rid, lambda item: value(item, 1).get("update_id") == 1, "update not received")
                        update = value(data, 1)
                        assert update["command"] == "start" and update["command_args"] == "hello" and update["chat"]["id"] == ALLOWED
                        wait(rid, lambda item: stats(item).get("dropped_chat") == 1, "dropped counter missing")
                        http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                            "action": "publish_output", "node_id": "cmd", "port_id": 1, "value": "Reply from graph",
                            "content_type": "text/plain"})
                        sent = value(wait(rid, lambda item: value(item, 2).get("message_id"), "reply not sent"), 2)
                        assert sent["chat_id"] == ALLOWED and api.sent[-1]["text"] == "Reply from graph"
                        polls = [call for call in api.calls if call["method"] == "getUpdates"]
                        assert polls and polls[0]["params"]["allowed_updates"] == ["message"]
                    finally:
                        stop(rid)
                    assert json.loads(state_file.read_text())["bots"][str(BOT["id"])]["offset"] == 3
                    if full:
                        api.queue(message(3, ALLOWED, "second run"))
                        before = len(api.calls)
                        rid = start()
                        try:
                            wait(rid, lambda item: value(item, 1).get("update_id") == 3, "restart update missing")
                            first = next(call for call in api.calls[before:] if call["method"] == "getUpdates")
                            assert first["params"]["offset"] == 3, "A restart must resume after the stored offset"
                            http_json(server.base_url, "/api/application/secrets/lock", method="POST", payload={})
                            http_json(server.base_url, f"/api/runs/{rid}/active/control", method="POST", payload={
                                "action": "publish_output", "node_id": "cmd", "port_id": 1, "value": "locked",
                                "content_type": "text/plain"})
                            wait(rid, lambda item: value(item, 3).get("code") == "secret", "locked wallet not reported")
                            assert api.sent[-1]["text"] == "Reply from graph"
                        finally:
                            stop(rid)
                assert TOKEN not in "\n".join(server.logs)
            print("[ok] Telegram graph " + mode + " " + str(origin), flush=True)


def main():
    send_protocol()
    receive_protocol()
    graph_runs()


if __name__ == "__main__":
    main()
