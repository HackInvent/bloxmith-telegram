"""FB1/FB2/FB3/FB4/FB6: pure settings, inputs, normalization, errors, storage and file policy.

No socket is opened and no provider is contacted. Temporary directories hold the
file-policy and offset-storage cases.
"""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import MappingProxyType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(Path(__file__).resolve().parent)]
from blocs.telegram import logic, storage, transport
from blocs.telegram.block import TelegramBlock
from bloxsmith_app.block_api import BlockRuntimeContext, BlockRuntimePreparationContext
from telegram_fixture import ALLOWED, BOT, GROUP, OTHER, REF, TOKEN, message

BASE = {"enabled": True, "secret_ref": REF, "allowed_chat_ids": [ALLOWED, GROUP], "default_chat_id": str(ALLOWED)}


def rejected(call, code=None):
    """A contract violation must raise a coded error, never fall back silently."""
    try:
        call()
    except logic.TelegramError as exc:
        assert code is None or exc.code == code, (code, exc.code, str(exc))
        assert TOKEN not in str(exc)
        return exc
    raise AssertionError("Invalid value accepted")


def settings_contract():
    """# - FB1 - remote access stays off by default and every setting is bounded."""
    defaults = logic.configuration({})
    assert defaults["enabled"] is False and defaults["api_base_url"] == logic.OFFICIAL_API
    assert defaults["default_chat_id"] is None and defaults["allowed_chat_ids"] == []
    config = logic.configuration({**BASE, "allowed_chat_ids": [str(ALLOWED), GROUP], "execution": {"inhibited": False}})
    assert config["allowed_chat_ids"] == [ALLOWED, GROUP] and config["default_chat_id"] == ALLOWED
    frozen = logic.configuration(MappingProxyType({**BASE, "allowed_chat_ids": (ALLOWED,), "update_types": ("message",)}))
    assert frozen["allowed_chat_ids"] == [ALLOWED] and frozen["update_types"] == ["message"]
    for raw in ({"enabled": True}, {"enabled": True, "secret_ref": REF}, {**BASE, "secret_ref": ""},
                {**BASE, "secret_ref": TOKEN}, {**BASE, "secret_ref": "env://TELEGRAM"},
                {**BASE, "allowed_chat_ids": [True]}, {**BASE, "allowed_chat_ids": [0]},
                {**BASE, "allowed_chat_ids": [2 ** 53]}, {**BASE, "allowed_chat_ids": [ALLOWED, ALLOWED]},
                {**BASE, "allowed_chat_ids": "1001"}, {**BASE, "allowed_chat_ids": ["@channel"]},
                {**BASE, "allowed_chat_ids": list(range(1, 102))}, {**BASE, "default_chat_id": str(OTHER)},
                {**BASE, "default_chat_id": "abc"}, {**BASE, "parse_mode": "Markdown"}, {**BASE, "file_mode": "video"},
                {**BASE, "update_types": []}, {**BASE, "update_types": ["callback_query"]},
                {**BASE, "update_types": ["message", "message"]}, {**BASE, "enabled": "yes"},
                {**BASE, "receive_updates": 1}, {**BASE, "file_root": "relative/dir"}, {**BASE, "file_root": "/srv/../etc"},
                {**BASE, "ca_file": "ca.pem"}, {**BASE, "token": TOKEN}, {**BASE, "api_base_url": "http://api.telegram.org"},
                {**BASE, "api_base_url": "https://api.telegram.org/bot"}, {**BASE, "api_base_url": "https://user:pw@api.telegram.org"},
                {**BASE, "api_base_url": "https://api.telegram.org/?x=1"}, {**BASE, "api_base_url": "ftp://api.telegram.org"},
                {**BASE, "api_base_url": "https://api telegram.org"}, {**BASE, "api_base_url": "https://api.telegram.org:0"},
                {**BASE, "api_base_url": "https://-bad-.example"}):
        error = rejected(lambda raw=raw: logic.configuration(raw), "config")
        assert TOKEN not in json.dumps(error.envelope())
    for key, (low, high) in logic.BOUNDS.items():
        for bad in (low - 1, high + 1, True, float(low), str(low)):
            rejected(lambda key=key, bad=bad: logic.configuration({**BASE, key: bad}), "config")
        assert logic.configuration({**BASE, key: low})[key] == low
        assert logic.configuration({**BASE, key: high})[key] == high
    origins = {"https://api.telegram.org/": "https://api.telegram.org", "http://127.0.0.1:8081": "http://127.0.0.1:8081",
               "http://localhost:9": "http://localhost:9", "http://[::1]:7": "http://[::1]:7",
               "https://Bot.Example.test:8443": "https://bot.example.test:8443"}
    for raw, expected in origins.items():
        assert logic.configuration({**BASE, "api_base_url": raw})["api_base_url"] == expected
    assert logic.bot_token(" " + TOKEN + "\n") == TOKEN
    for bad in ("", "123", "abc:" + "x" * 30, "123:" + "x" * 10, "123:" + "x" * 30 + "/../", "123:" + "x" * 30 + " y", None):
        rejected(lambda bad=bad: logic.bot_token(bad), "secret")
    redacted = logic.redact("GET /bot" + TOKEN + "/sendMessage " + TOKEN + " " + TOKEN.split(":")[1], TOKEN)
    assert TOKEN not in redacted and TOKEN.split(":")[1] not in redacted and "/bot***/" in redacted


def input_contract():
    """# - FB2 - one explicit message or file request for an allowlisted chat only."""
    config = logic.configuration(BASE)
    plain = logic.message_request("Hello <b>team</b>", config)
    assert plain["params"] == {"chat_id": ALLOWED, "text": "Hello <b>team</b>"} and plain["method"] == "sendMessage"
    rich = logic.message_request(json.dumps({"chat_id": str(GROUP), "text": "*Hi*", "parse_mode": "MarkdownV2",
        "disable_notification": True, "disable_link_preview": True, "reply_to_message_id": 9,
        "message_thread_id": 4, "request_id": "order-1"}), config)
    assert rich["params"] == {"chat_id": GROUP, "text": "*Hi*", "parse_mode": "MarkdownV2", "disable_notification": True,
        "reply_parameters": {"message_id": 9, "allow_sending_without_reply": True}, "message_thread_id": 4,
        "link_preview_options": {"is_disabled": True}}
    assert rich["request_id"] == "order-1" and rich["chat_id"] == GROUP
    html = logic.message_request({"text": "<i>x</i>"}, logic.configuration({**BASE, "parse_mode": "HTML"}))
    assert html["params"]["parse_mode"] == "HTML"
    rejected(lambda: logic.message_request({"text": "hi", "chat_id": OTHER}, config), "chat_not_allowed")
    rejected(lambda: logic.message_request("hi", logic.configuration({**BASE, "default_chat_id": ""})), "chat_required")
    for bad in ("   ", "x" * 4097, "a\x00b", '{"text":"x"', '{"text":"x","text":"y"}', '{"text": NaN}',
                {"text": "x", "token": "y"}, {"text": "x", "parse_mode": "Markdown"}, {"text": 5},
                {"text": "x", "disable_notification": "true"}, {"text": "x", "reply_to_message_id": 0},
                {"text": "x", "message_thread_id": True}, {"text": "x", "request_id": "bad id"},
                {"text": "x", "chat_id": "@channel"}, {"text": "x", "chat_id": 1.5}, b"\xff", 42,
                "x" * 70000):
        rejected(lambda bad=bad: logic.message_request(bad, config), "invalid_input")
    assert logic.message_request("x" * 4096, config)["params"]["text"] == "x" * 4096
    assert logic.message_request("not {json", config)["params"]["text"] == "not {json"
    assert logic.message_request('["only", "text"]', config)["params"]["text"] == '["only", "text"]'
    upload = logic.file_request("reports/q3.pdf", config)
    assert upload["path"] == "reports/q3.pdf" and upload["params"] == {"chat_id": ALLOWED} and upload["as"] is None
    upload = logic.file_request({"path": "a.png", "caption": "*Q3*", "parse_mode": "MarkdownV2", "as": "photo",
                                 "filename": "résumé.png", "chat_id": GROUP}, config)
    assert upload["params"] == {"chat_id": GROUP, "parse_mode": "MarkdownV2", "caption": "*Q3*"}
    assert upload["filename"] == "résumé.png" and upload["as"] == "photo"
    assert "parse_mode" not in logic.file_request({"path": "a.pdf", "parse_mode": "HTML"}, config)["params"]
    for bad in ({"path": ""}, {"path": "a\nb"}, {"path": "a", "caption": "x" * 1025}, {"path": "a", "as": "video"},
                {"path": "a", "filename": "../x"}, {"path": "a", "filename": 'a"b'}, {"path": "a", "extra": 1}, {"caption": "x"}):
        rejected(lambda bad=bad: logic.file_request(bad, config), "invalid_input")
    rejected(lambda: logic.file_request({"path": "a", "chat_id": OTHER}, config), "chat_not_allowed")
    request = logic.file_request("a.jpg", config)
    assert logic.file_method(request, config, 1024, ".JPG") == ("sendPhoto", "photo")
    roomy = logic.configuration({**BASE, "max_file_bytes": 20 * 1024 * 1024})
    assert logic.file_method(request, roomy, logic.PHOTO_LIMIT + 1, ".jpg") == ("sendDocument", "document")
    rejected(lambda: logic.file_method(request, config, logic.PHOTO_LIMIT + 1, ".jpg"), "file_too_large")
    assert logic.file_method(request, config, 10, ".pdf") == ("sendDocument", "document")
    assert logic.file_method({**request, "as": "document"}, config, 10, ".png") == ("sendDocument", "document")
    rejected(lambda: logic.file_method({**request, "as": "photo"}, roomy, logic.PHOTO_LIMIT + 1, ".png"), "file_too_large")
    small = logic.configuration({**BASE, "max_file_bytes": 2048})
    rejected(lambda: logic.file_method(request, small, 4096, ".pdf"), "file_too_large")
    assert logic.multipart_fields({"chat_id": GROUP, "disable_notification": True,
                                   "reply_parameters": {"message_id": 3}}) == [
        ["chat_id", str(GROUP)], ["disable_notification", "true"], ["reply_parameters", '{"message_id":3}']]


def provider_contract():
    """# - FB3 - documented error envelopes map to stable codes, bounded text and no token."""
    ok = logic.provider_result({"http_status": 200, "body": {"ok": True, "result": {"id": 1}}})
    assert ok == {"id": 1}
    cases = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
             413: "payload_too_large", 429: "rate_limited", 500: "provider_unavailable", 502: "provider_unavailable",
             418: "provider_error"}
    for status, code in cases.items():
        body = {"ok": False, "error_code": status, "description": "Bad\nthing for bot" + TOKEN + " \x07"}
        error = rejected(lambda status=status, body=body: logic.provider_result({"http_status": status, "body": body}, TOKEN), code)
        assert error.uncertain == (code == "provider_unavailable")
        assert TOKEN not in json.dumps(error.envelope()) and "\n" not in error.description and "\x07" not in error.description
    limited = rejected(lambda: logic.provider_result({"http_status": 429, "retry_after_header": 9, "body": {
        "ok": False, "error_code": 429, "description": "Too Many Requests: retry after 5", "parameters": {"retry_after": 5}}}),
        "rate_limited")
    assert limited.retry_after == 5 and limited.envelope()["retry_after"] == 5
    header = rejected(lambda: logic.provider_result({"http_status": 429, "retry_after_header": 9,
                                                     "body": {"ok": False, "error_code": 429}}), "rate_limited")
    assert header.retry_after == 9
    migrated = rejected(lambda: logic.provider_result({"http_status": 400, "body": {"ok": False, "error_code": 400,
        "description": "Bad Request: group chat was upgraded to a supergroup chat",
        "parameters": {"migrate_to_chat_id": -1001234567890}}}), "bad_request")
    assert migrated.envelope()["migrate_to_chat_id"] == -1001234567890
    rejected(lambda: logic.provider_result({"http_status": 502, "body": None}), "provider_unavailable")
    rejected(lambda: logic.provider_result({"http_status": 200, "body": {"result": 1}}), "protocol")
    rejected(lambda: logic.provider_result({"http_status": "200", "body": {}}), "protocol")
    # Worker outcomes keep the "nothing sent" versus "uncertain" distinction.
    for outcome, code, uncertain in (({"status": "error", "code": "timeout", "phase": "receive"}, "timeout", True),
                                     ({"status": "error", "code": "timeout", "phase": "send"}, "timeout", False),
                                     ({"status": "error", "code": "network", "phase": "connect"}, "network", False),
                                     ({"status": "error", "code": "network", "phase": "receive"}, "network", True),
                                     ({"status": "error", "code": "tls", "phase": "connect"}, "tls", False),
                                     ({"status": "error", "code": "file_changed", "phase": "send"}, "file_changed", False),
                                     ({"status": "response", "http_status": 200, "body_error": "too_large"}, "protocol", True),
                                     ({"status": "response", "http_status": 503, "body_error": "not_json"}, "provider_unavailable", True),
                                     ({"status": "response", "http_status": 429, "retry_after_header": 4, "body_error": "not_json"}, "rate_limited", False),
                                     ({"status": "bogus"}, "protocol", False)):
        error = rejected(lambda outcome=outcome: transport.interpret(outcome, TOKEN, mutation=True), code)
        assert error.uncertain is uncertain, (outcome, error.uncertain)
    assert not rejected(lambda: transport.interpret({"status": "error", "code": "network", "phase": "receive"}, TOKEN,
                                                    mutation=False), "network").uncertain
    assert logic.backoff_delay(1, "network") == 1 and logic.backoff_delay(9, "network") == 60
    assert logic.backoff_delay(1, "rate_limited", 7) == 7 and logic.backoff_delay(1, "rate_limited", 9000) == 300
    assert logic.backoff_delay(1, "unauthorized") == 30 and logic.backoff_delay(1, "conflict") == 15
    budget = logic.RateBudget(2, 100.0)
    assert budget.take(100.0) and budget.take(100.0) and not budget.take(100.0)
    assert 29 < budget.wait_time(100.0) <= 30 and budget.take(130.5)


def update_contract():
    """# - FB4 - normalized updates, allowlist drops and private-field exclusion."""
    config = logic.configuration({**BASE, "receive_updates": True})
    decision, update = logic.normalize_update(message(10, ALLOWED, "/Start@fixture_bot  order 42"), config, BOT["username"])
    assert decision == "accepted" and update["command"] == "start" and update["command_args"] == "order 42"
    assert update["chat"] == {"id": ALLOWED, "type": "private", "first_name": "Ada"} and update["sender"]["username"] == "ada"
    assert update["date"] == "2025-10-09T08:53:30Z" and update["content_type"] == "text" and update["files"] == []
    assert update["update_id"] == 10 and update["message_id"] == 510 and update["type"] == "message"
    assert logic.normalize_update(message(11, ALLOWED, "/start@other_bot"), config, "fixture_bot")[1]["command"] is None
    assert logic.normalize_update(message(12, ALLOWED, "hello /start"), config, "fixture_bot")[1]["command"] is None
    assert logic.normalize_update(message(13, OTHER, "intruder"), config, "fixture_bot") == ("dropped_chat", None)
    assert logic.normalize_update(message(14, ALLOWED, "x", kind="edited_message"), config) == ("dropped_type", None)
    assert logic.normalize_update({"update_id": 15, "callback_query": {"id": "1"}}, config) == ("dropped_type", None)
    for bad in ({"update_id": -1, "message": {}}, {"update_id": "16"}, {"update_id": 17, "message": {"message_id": 1}},
                {"update_id": 18, "message": {"chat": {"id": ALLOWED, "type": "private"}}}, [], None):
        assert logic.normalize_update(bad, config)[0] in ("invalid", "dropped_type"), bad
    extended = logic.configuration({**BASE, "update_types": ["message", "edited_message", "channel_post"]})
    edited = logic.normalize_update(message(19, ALLOWED, "fixed", kind="edited_message", extra={"edit_date": 1760000100}), extended)[1]
    assert edited["type"] == "edited_message" and edited["edit_date"] == "2025-10-09T08:55:00Z"
    post = message(20, GROUP, "news", kind="channel_post")
    post["channel_post"].pop("from")
    post["channel_post"]["sender_chat"] = {"id": GROUP, "type": "channel", "title": "News"}
    assert logic.normalize_update(post, extended)[1]["sender_chat"]["title"] == "News"
    media = message(21, ALLOWED, None, extra={"text": None, "caption": "Q3", "photo": [
        {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 90, "file_size": 10},
        {"file_id": "large", "file_unique_id": "l", "width": 1280, "height": 960, "file_size": 99}],
        "document": {"file_id": "doc", "file_unique_id": "d", "file_name": "q3.pdf", "mime_type": "application/pdf", "file_size": 5}})
    normalized = logic.normalize_update(media, config)[1]
    assert normalized["content_type"] == "photo" and normalized["caption"] == "Q3" and normalized["text"] is None
    assert [item["file_id"] for item in normalized["files"]] == ["large", "doc"] and normalized["files"][1]["file_name"] == "q3.pdf"
    place = logic.normalize_update(message(22, ALLOWED, None, extra={"text": None, "location": {"latitude": 48.85, "longitude": 2.35}}), config)[1]
    assert place["content_type"] == "location" and place["location"] == {"latitude": 48.85, "longitude": 2.35}
    contact = logic.normalize_update(message(23, ALLOWED, None, extra={"text": None, "contact": {"phone_number": "+33600000000",
        "first_name": "Bob"}}), config)[1]
    assert contact["content_type"] == "unsupported" and "+33600000000" not in json.dumps(contact)
    receipt = logic.normalize_sent({"message_id": 7, "date": 1760000000, "chat": {"id": ALLOWED, "type": "private"},
                                    "text": "secret content"}, {"request_id": "r1"}, "sendMessage", 2)
    assert receipt == {"request_id": "r1", "operation": "send_message", "chat_id": ALLOWED, "chat_type": "private",
                       "message_id": 7, "date": "2025-10-09T08:53:20Z", "attempts": 1 + 1}
    document = logic.normalize_sent({"message_id": 8, "chat": {"id": ALLOWED, "type": "private"}, "document": {
        "file_id": "f", "file_unique_id": "u", "file_size": 3, "file_name": "a.pdf"}}, {}, "sendDocument", 1)
    assert document["file"]["file_name"] == "a.pdf" and document["operation"] == "send_document"
    error = rejected(lambda: logic.normalize_sent({"ok": True}, {}, "sendMessage", 1), "protocol")
    assert error.uncertain
    now = 1_800_000_000
    assert logic.usable_offset({"offset": 41, "updated_at": now - 60}, now) == 41
    assert logic.usable_offset({"offset": 41, "updated_at": now - 86401}, now) is None
    assert logic.usable_offset({"offset": 41, "updated_at": now + 3600}, now) is None
    assert logic.usable_offset({"offset": "41", "updated_at": now}, now) is None and logic.usable_offset(None, now) is None


def storage_contract():
    """# - FB4 - offsets are atomic, private, bounded and never guessed when malformed."""
    with tempfile.TemporaryDirectory() as folder:
        directory = Path(folder)
        store, rival = storage.OffsetStore(directory), storage.OffsetStore(directory)
        assert store.acquire() and not rival.acquire()
        assert store.load() == {"schema_version": 1, "bots": {}}
        document = storage.record(store.load(), BOT["id"], 42, 1_800_000_000)
        store.save(document)
        assert store.load() == document
        state = directory / storage.STATE_NAME
        assert stat.S_IMODE(state.stat().st_mode) == 0o600 and TOKEN not in state.read_text()
        many = document
        for index in range(20):
            many = storage.record(many, 1000 + index, index, 1_800_000_001 + index)
        assert len(many["bots"]) == 16 and "1019" in many["bots"] and str(BOT["id"]) not in many["bots"]
        for broken in ("{", '{"schema_version":1,"bots":{"x":{"offset":1,"updated_at":2}}}',
                       '{"schema_version":2,"bots":{}}', '{"schema_version":1,"bots":{"1":{"offset":-1,"updated_at":2}}}',
                       '{"schema_version":1,"bots":{},"extra":1}', "x" * 20000):
            state.write_text(broken)
            rejected(store.load, "state_invalid")
        state.unlink()
        target = directory / "elsewhere.json"
        target.write_text(json.dumps(document))
        state.symlink_to(target)
        rejected(store.load, "state_invalid")
        store.close()
        assert rival.acquire()
        rival.close()


def file_policy_contract():
    """# - FB2 - only single-link regular files inside file_root can be uploaded."""
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        root, outside = base / "share", base / "private"
        (root / "reports").mkdir(parents=True)
        outside.mkdir()
        (root / "reports" / "q3.pdf").write_bytes(b"%PDF-1.4 fixture")
        (root / "photo.jpg").write_bytes(b"\xff\xd8fixture")
        (outside / "secret.txt").write_text("private")
        (root / "link.txt").symlink_to(outside / "secret.txt")
        (root / "linked-dir").symlink_to(outside)
        os.link(root / "photo.jpg", root / "hardlink.jpg")
        os.mkfifo(root / "pipe")
        (root / "big.bin").write_bytes(b"x" * 4096)
        config = logic.configuration({**BASE, "file_root": str(root), "max_file_bytes": 2048})
        found = transport.locate("reports/q3.pdf", config)
        assert found["parts"] == ["reports", "q3.pdf"] and found["size"] == 16 and found["suffix"] == ".pdf"
        assert transport.locate(str(root / "reports" / "q3.pdf"), config)["parts"] == ["reports", "q3.pdf"]
        for path, code in (("../private/secret.txt", "file_not_allowed"), (str(outside / "secret.txt"), "file_not_allowed"),
                           ("link.txt", "file_not_allowed"), ("linked-dir/secret.txt", "file_not_allowed"),
                           ("hardlink.jpg", "file_not_allowed"), ("pipe", "file_not_allowed"), ("reports", "file_not_allowed"),
                           ("missing.pdf", "file_not_found"), ("big.bin", "file_too_large"), (".", "file_not_allowed")):
            rejected(lambda path=path: transport.locate(path, config), code)
        rejected(lambda: transport.locate("a.pdf", logic.configuration(BASE)), "file_root_required")
        rejected(lambda: transport.locate("a.pdf", logic.configuration({**BASE, "file_root": str(base / "missing")})), "file_root_required")
        rejected(lambda: transport.locate("etc/hostname", logic.configuration({**BASE, "file_root": "/"})), "file_root_required")


def block_contract():
    """# - FB3 - # - FB5 - # - FB6 - block hooks fail before side effects and keep surfaces bounded."""
    block = TelegramBlock()
    ports_in = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["inputs"])
    ports_out = tuple(SimpleNamespace(id=port["id"], name=port["name"]) for port in block.model["ports"]["outputs"])
    calls = []

    def resolver(ref):
        calls.append(ref)
        return "not a token " + TOKEN

    def run(config, inputs):
        context = BlockRuntimeContext(config={**block.default_config(), **config}, inputs=inputs, input_ports=ports_in,
                                      output_ports=ports_out, services={"resolve_secret": resolver})
        result = block.execute_runtime(context)
        assert result.status in ("success", "skipped"), result
        return result, {output.port_name: json.loads(output.value) for output in result.outputs}

    assert run(BASE, {})[0].status == "skipped"
    for config, inputs, code in (({**BASE, "enabled": False}, {"message": "hi"}, "disabled"),
                                 (BASE, {"message": "hi", "file": "a.pdf"}, "one_operation"),
                                 (BASE, {"message": {"text": "hi", "chat_id": OTHER}}, "chat_not_allowed"),
                                 (BASE, {"file": "a.pdf"}, "file_root_required"),
                                 (BASE, {"message": '{"text":'}, "invalid_input"),
                                 ({**BASE, "default_chat_id": ""}, {"message": "hi"}, "chat_required")):
        result, outputs = run(config, inputs)
        assert set(outputs) == {"error"} and outputs["error"]["code"] == code, outputs
        assert outputs["error"]["source"] == "send" and result.metadata["telegram"]["send"]["code"] == code
    assert not calls, "The wallet must not be read before input and policy validation"
    result, outputs = run(BASE, {"message": {"text": "hi", "request_id": "req-1"}})
    assert outputs["error"]["code"] == "secret" and outputs["error"]["request_id"] == "req-1" and calls == [REF]
    assert TOKEN not in json.dumps(outputs) and TOKEN not in " ".join(result.logs)

    def prepared(config, mode):
        context = BlockRuntimePreparationContext.from_context(SimpleNamespace(config=config, runtime_mode=mode))
        return block.prepare_runtime(context).listen_on_run

    assert prepared({**BASE, "receive_updates": True}, "zeromq_active") is True
    assert prepared({**BASE, "receive_updates": True}, "centralized") is False
    assert prepared({**BASE, "receive_updates": True, "enabled": False}, "zeromq_active") is False
    assert prepared(BASE, "zeromq_active") is False
    rejected(lambda: prepared({**BASE, "secret_ref": TOKEN}, "zeromq_active"), "config")
    node = block.build_node_payload(node_id="bot", position={"x": 0, "y": 0})
    accepted = block.handle_ui_action(node=node, action="save_settings", values={"node_patch": {"config": BASE}})
    operation = accepted["graph_operations"][-1]
    assert operation["op"] == "update_node_config" and operation["replace"] is True
    assert operation["config"]["allowed_chat_ids"] == BASE["allowed_chat_ids"]
    refused = block.handle_ui_action(node=node, action="save_settings",
                                     values={"node_patch": {"config": {**BASE, "secret_ref": TOKEN}}})
    assert "secret_ref" in refused["error"] and TOKEN not in refused["error"] and "graph_operations" not in refused
    html = block.render_modal(node=node)["html"]
    assert 'data-block-config-field="secret_ref"' in html and 'data-telegram-show="receive" hidden' in html
    assert 'data-block-value-type="json"' in html and "{{" not in html
    receiving = {**node, "config": {**node["config"], **BASE, "receive_updates": True}}
    assert 'data-telegram-show="receive" hidden' not in block.render_inspector_panel(node=receiving)["html"]
    assert block.render_node_card(node=node)["html"].count("Remote access off") == 1
    assert "Send and receive · 2 chats" in block.render_node_card(node=receiving)["html"]
    single = {**node, "config": {**node["config"], **BASE, "allowed_chat_ids": [ALLOWED]}}
    assert "Send only · 1 chat<" in block.render_node_card(node=single)["html"]
    # The preview keeps its catalog key and count so the editor can show it in the interface language.
    assert 'data-i18n="block.telegram.card_disabled" data-i18n-params=""' in block.render_node_card(node=node)["html"]
    assert ('data-i18n="block.telegram.card_receive" data-i18n-params="{&quot;count&quot;: 2}"'
            in block.render_node_card(node=receiving)["html"])
    assert 'data-i18n="block.telegram.card_send"' in block.render_node_card(node=single)["html"]
    assert REF not in block.render_node_card(node=receiving)["html"]


def main():
    settings_contract()
    input_contract()
    provider_contract()
    update_contract()
    storage_contract()
    file_policy_contract()
    block_contract()
    print("[ok] Telegram settings, inputs, provider envelopes, updates, storage, file policy and hooks")


if __name__ == "__main__":
    main()
