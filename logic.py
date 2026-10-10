"""Pure Telegram Bot API contracts: settings, inputs, normalization and errors.

No network, filesystem, framework or third-party import happens here. Every
function is deterministic, so the policy can be checked directly and the
isolated network worker never depends on the framework.
"""

from collections.abc import Mapping
from datetime import datetime, timezone
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit

OFFICIAL_API = "https://api.telegram.org"
DEFAULTS = {
    "enabled": False, "secret_ref": "", "allowed_chat_ids": [], "default_chat_id": "",
    "parse_mode": "none", "disable_notification": False, "disable_link_preview": False,
    "receive_updates": False, "update_types": ["message"], "file_root": "", "file_mode": "auto",
    "timeout_sec": 30, "poll_timeout_sec": 25, "poll_limit": 20, "max_updates_per_minute": 30,
    "max_file_bytes": 10485760, "max_response_bytes": 1048576, "max_retries": 2,
    "max_retry_wait_sec": 20, "api_base_url": OFFICIAL_API, "ca_file": "",
}
FRAMEWORK_KEYS = {"execution", "runtime_path", "runtime_path_label"}
BOUNDS = {
    "timeout_sec": (5, 300), "poll_timeout_sec": (1, 50), "poll_limit": (1, 100),
    "max_updates_per_minute": (1, 120), "max_file_bytes": (1024, 52428800),
    "max_response_bytes": (65536, 8388608), "max_retries": (0, 5), "max_retry_wait_sec": (1, 120),
}
PARSE_MODES = ("none", "MarkdownV2", "HTML")
UPDATE_TYPES = ("message", "edited_message", "channel_post", "edited_channel_post")
FILE_MODES = ("auto", "document", "photo")
METHODS = ("getMe", "getUpdates", "sendMessage", "sendDocument", "sendPhoto")
OPERATIONS = {"sendMessage": "send_message", "sendDocument": "send_document", "sendPhoto": "send_photo",
              "getMe": "get_me", "getUpdates": "get_updates"}
SECRET_REF = re.compile(r"secret://(?:workspace/[A-Za-z0-9_.-]{1,80}|project/[A-Za-z0-9_.-]{1,80}/[A-Za-z0-9_.-]{1,80})\Z")
BOT_TOKEN = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,128}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
CHAT_TEXT = re.compile(r"-?[1-9][0-9]{0,15}\Z")
COMMAND = re.compile(r"/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]{3,32}))?(?=\s|\Z)")
HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_CHAT_ID = 2 ** 53 - 1
MAX_MESSAGE_ID = 2 ** 31 - 1
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
PHOTO_LIMIT = 10 * 1024 * 1024
PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png")
INPUT_LIMIT = 65536
OFFSET_LIFETIME_SEC = 86400
MESSAGE_KEYS = {"chat_id", "text", "parse_mode", "disable_notification", "disable_link_preview",
                "reply_to_message_id", "message_thread_id", "request_id"}
FILE_KEYS = {"path", "chat_id", "caption", "parse_mode", "disable_notification",
             "reply_to_message_id", "message_thread_id", "request_id", "as", "filename"}
FILE_KINDS = ("document", "voice", "audio", "video", "animation", "video_note", "sticker")

MESSAGES = {
    "config": "Invalid Telegram settings.",
    "disabled": "Remote access is disabled; enable the block explicitly before sending.",
    "secret": "Unlock the wallet and store a Telegram bot token under the configured secret_ref.",
    "invalid_input": "The input does not follow the documented message contract.",
    "chat_required": "No chat_id was given and no default_chat_id is configured; nothing was sent.",
    "chat_not_allowed": "The chat is not listed in allowed_chat_ids; nothing was sent.",
    "file_root_required": "Configure file_root before sending local files; nothing was sent.",
    "file_not_found": "The file does not exist inside file_root; nothing was sent.",
    "file_not_allowed": "The file is outside file_root, is not a single-link regular file or passes through a symbolic link.",
    "file_too_large": "The file exceeds max_file_bytes (10 MiB for photos); nothing was sent.",
    "file_changed": "The file changed while it was uploaded; delivery was aborted before completion.",
    "one_operation": "Send one message or one file per activation; nothing was sent.",
    "bad_request": "Telegram rejected the request (HTTP 400); nothing was sent.",
    "unauthorized": "Telegram rejected the bot token (HTTP 401); check the wallet secret.",
    "forbidden": "The bot may not act in this chat (HTTP 403); nothing was sent.",
    "not_found": "Telegram returned HTTP 404; check the bot token and api_base_url.",
    "conflict": "Another getUpdates poller or an active webhook uses this bot (HTTP 409).",
    "payload_too_large": "Telegram refused the request size (HTTP 413); nothing was sent.",
    "rate_limited": "Telegram rate limit reached (HTTP 429); retry after the indicated delay.",
    "provider_error": "Telegram returned an unexpected error status; nothing is assumed sent.",
    "provider_unavailable": "Telegram is temporarily unavailable (HTTP 5xx); delivery is uncertain.",
    "network": "The Telegram API could not be reached; nothing was sent.",
    "network_uncertain": "The connection failed after the request was sent; delivery is uncertain.",
    "tls": "TLS verification failed for the Telegram API origin; nothing was sent.",
    "timeout": "The Telegram operation exceeded timeout_sec before the request was complete; nothing was sent.",
    "timeout_uncertain": "The Telegram operation exceeded timeout_sec after the request was sent; delivery is uncertain.",
    "cancelled": "The operation was cancelled; delivery is uncertain.",
    "cancelled_before_send": "The operation was cancelled before any request was sent.",
    "protocol": "Telegram returned an invalid, compressed or oversized response.",
    "storage": "Private node storage is unavailable; updates are not polled.",
    "state_invalid": "The stored update offset is invalid; updates are not polled until it is repaired or removed.",
    "busy": "Another listener already polls updates for this node storage.",
    "internal": "The Telegram operation failed without a complete result.",
}


class TelegramError(ValueError):
    """A content-free diagnostic with a stable machine code and an English message."""

    def __init__(self, code, detail=None, *, uncertain=False, http_status=None, retry_after=None,
                 description=None, migrate_to_chat_id=None):
        super().__init__(detail or MESSAGES.get(code, MESSAGES["internal"]))
        self.code, self.uncertain = code, bool(uncertain)
        self.http_status, self.retry_after = http_status, retry_after
        self.description, self.migrate_to_chat_id = description, migrate_to_chat_id
        self.attempts = 0

    def envelope(self, **context):
        """Return the public error document; callers add only identifiers, never content."""
        result = {"code": self.code, "message": str(self), "uncertain": self.uncertain}
        for key, value in (("http_status", self.http_status), ("retry_after", self.retry_after),
                           ("provider_description", self.description),
                           ("migrate_to_chat_id", self.migrate_to_chat_id)):
            if value is not None:
                result[key] = value
        if self.attempts:
            result["attempts"] = self.attempts
        for key, value in context.items():
            if value is not None:
                result[key] = value
        return result


def fail(code, detail=None, **extra):
    """Raise a structured failure before any side effect."""
    raise TelegramError(code, detail, **extra)


def thaw(value):
    """Turn immutable preparation snapshots back into plain JSON containers."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def encoded(value, limit=INPUT_LIMIT, code="invalid_input"):
    """Serialize strict finite JSON with bounded depth, element count and bytes."""
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 24 or count > 50000:
            fail(code, "JSON exceeds its depth or element limit.")
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                fail(code, "JSON object keys must be strings.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (str, int, float, bool):
            fail(code, "A strict JSON value is required.")
        elif type(item) is float and not math.isfinite(item):
            fail(code, "Non-finite numbers are not accepted.")
    try:
        data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail(code, "Finite UTF-8 JSON is required.")
    if len(data) > limit:
        fail(code, "JSON exceeds its byte limit.")
    return data


def strict_json(text, limit=INPUT_LIMIT, code="invalid_input"):
    """Parse JSON text, rejecting duplicate keys, non-finite numbers and oversized input."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="strict")
        except UnicodeError:
            fail(code, "UTF-8 JSON is required.")
    if not isinstance(text, str):
        fail(code, "JSON text is required.")
    if len(text.encode("utf-8", errors="replace")) > limit:
        fail(code, "JSON exceeds its byte limit.")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                fail(code, "Duplicate JSON property.")
            result[key] = item
        return result

    def constant(_name):
        fail(code, "Non-finite numbers are not accepted.")

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except TelegramError:
        raise
    except (ValueError, RecursionError):
        fail(code, "Malformed JSON.")
    encoded(value, limit, code)
    return value


def boolean(value, name, code="config"):
    if type(value) is not bool:
        fail(code, f"{name} must be a boolean.")
    return value


def integer(value, name, low, high, code="config"):
    if type(value) is not int or not low <= value <= high:
        fail(code, f"{name} must be an integer between {low} and {high}.")
    return value


def chat_id(value, name="chat_id", code="invalid_input"):
    """Normalize one numeric Telegram chat ID; usernames are not accepted as targets."""
    if type(value) is int:
        number = value
    elif isinstance(value, str) and CHAT_TEXT.fullmatch(value.strip()):
        number = int(value.strip())
    else:
        fail(code, f"{name} must be a numeric Telegram chat ID, such as 123456789 or -1001234567890.")
    if not 1 <= abs(number) <= MAX_CHAT_ID:
        fail(code, f"{name} is outside the Telegram chat ID range.")
    return number


def path_text(value, name, code="config"):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        fail(code, f"{name} must be a bounded path without control characters.")
    value = value.strip()
    if value and (not value.startswith("/") or ".." in value.split("/")):
        fail(code, f"{name} must be empty or an absolute path without '..' components.")
    return value


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def api_origin(value):
    """Accept an HTTPS origin; plain HTTP only for a loopback host such as a local test fixture."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        fail("config", "api_base_url must be an ASCII URL without spaces.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        fail("config", "api_base_url has an invalid host or port.")
    host = (parsed.hostname or "").lower()
    if (parsed.scheme not in ("https", "http") or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or "?" in value or "#" in value
            or parsed.path not in ("", "/") or "\\" in value or "%" in host):
        fail("config", "api_base_url must be a bare https:// origin without credentials, path, query or fragment.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or host.endswith(".") or any(not HOST_LABEL.fullmatch(label) for label in host.split(".")):
            fail("config", "api_base_url has an invalid host name.")
    if port is not None and not 1 <= port <= 65535:
        fail("config", "api_base_url has an invalid port.")
    if parsed.scheme == "http" and not is_loopback(host):
        fail("config", "Plain HTTP is accepted only for a loopback test host; use HTTPS.")
    netloc = "[" + host + "]" if ":" in host else host
    return parsed.scheme + "://" + netloc + (":" + str(port) if port is not None else "")


def configuration(raw):
    """Pure settings validation; no wallet, filesystem or network access."""
    raw = thaw(raw)
    if not isinstance(raw, Mapping):
        fail("config", "Settings must be a JSON object.")
    unknown = set(raw) - set(DEFAULTS) - FRAMEWORK_KEYS
    if unknown:
        fail("config", "Unknown setting: " + sorted(unknown)[0] + ". Credentials belong in the wallet.")
    config = {**{key: thaw(value) for key, value in DEFAULTS.items()},
              **{key: value for key, value in raw.items() if key in DEFAULTS}}
    for key in ("enabled", "disable_notification", "disable_link_preview", "receive_updates"):
        boolean(config[key], key)
    for key, (low, high) in BOUNDS.items():
        integer(config[key], key, low, high)
    ref = config["secret_ref"]
    if not isinstance(ref, str) or len(ref) > 256:
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/telegram_bot.")
    ref = ref.strip()
    if ref and not SECRET_REF.fullmatch(ref):
        fail("config", "secret_ref must be a wallet reference such as secret://workspace/telegram_bot, never the token.")
    config["secret_ref"] = ref
    chats = config["allowed_chat_ids"]
    if not isinstance(chats, list) or len(chats) > 100:
        fail("config", "allowed_chat_ids must be a JSON array of at most 100 numeric chat IDs.")
    config["allowed_chat_ids"] = [chat_id(item, "allowed_chat_ids", "config") for item in chats]
    if len(set(config["allowed_chat_ids"])) != len(config["allowed_chat_ids"]):
        fail("config", "allowed_chat_ids must not contain duplicates.")
    default = config["default_chat_id"]
    if default in ("", None):
        config["default_chat_id"] = None
    else:
        config["default_chat_id"] = chat_id(default, "default_chat_id", "config")
        if config["default_chat_id"] not in config["allowed_chat_ids"]:
            fail("config", "default_chat_id must also be listed in allowed_chat_ids.")
    if config["parse_mode"] not in PARSE_MODES:
        fail("config", "parse_mode must be none, MarkdownV2 or HTML.")
    if config["file_mode"] not in FILE_MODES:
        fail("config", "file_mode must be auto, document or photo.")
    types = config["update_types"]
    if (not isinstance(types, list) or not 1 <= len(types) <= len(UPDATE_TYPES)
            or any(item not in UPDATE_TYPES for item in types) or len(set(types)) != len(types)):
        fail("config", "update_types must list distinct values among " + ", ".join(UPDATE_TYPES) + ".")
    config["file_root"] = path_text(config["file_root"], "file_root")
    config["ca_file"] = path_text(config["ca_file"], "ca_file")
    config["api_base_url"] = api_origin(config["api_base_url"])
    if config["enabled"]:
        if not config["secret_ref"]:
            fail("config", "Remote access requires secret_ref, a wallet reference holding the bot token.")
        if not config["allowed_chat_ids"]:
            fail("config", "Remote access requires at least one numeric chat ID in allowed_chat_ids.")
    return config


def bot_token(value):
    """Validate the wallet value before it reaches a URL path; never echo it."""
    if not isinstance(value, str) or not BOT_TOKEN.fullmatch(value.strip()):
        fail("secret", "The wallet secret is not a Telegram bot token (digits, colon, token characters).")
    return value.strip()


def redact(text, token):
    """Mask the token wherever it could appear, including the /bot<token>/ URL path."""
    text = "" if text is None else str(text)
    if token:
        text = text.replace("bot" + token, "bot***").replace(token, "***")
        if ":" in token:
            text = text.replace(token.split(":", 1)[1], "***")
    return text


def clean(value, limit):
    """Bounded single-line provider text with control characters removed."""
    if not isinstance(value, str):
        return None
    value = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).strip()
    return value[:limit] if value else None


def identifier(value):
    if value is None:
        return None
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail("invalid_input", "request_id needs 1-128 ASCII letters, digits or ._:- characters.")
    return value


def positive_id(value, name):
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= MAX_MESSAGE_ID:
        fail("invalid_input", f"{name} must be a positive integer.")
    return value


def structured(value, primary):
    """Read a JSON object, or plain text as {primary: text}; '{' must start valid JSON."""
    if isinstance(value, Mapping):
        document = thaw(value)
        encoded(document)
        return document
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeError:
            fail("invalid_input", "Inputs must be UTF-8 text or a JSON object.")
    if not isinstance(value, str):
        fail("invalid_input", "Inputs must be UTF-8 text or a JSON object.")
    if len(value.encode("utf-8", errors="replace")) > INPUT_LIMIT:
        fail("invalid_input", "The input exceeds 64 KiB.")
    if value.lstrip().startswith("{"):
        document = strict_json(value.strip())
        if not isinstance(document, dict):
            fail("invalid_input", "A value starting with '{' must be one JSON object.")
        return document
    return {primary: value}


def option(document, key, allowed, default):
    value = document.get(key, default)
    if value not in allowed:
        fail("invalid_input", f"{key} must be one of: " + ", ".join(allowed) + ".")
    return value


def target_chat(document, config):
    raw = document.get("chat_id")
    value = config["default_chat_id"] if raw is None else chat_id(raw)
    if value is None:
        fail("chat_required")
    if value not in config["allowed_chat_ids"]:
        fail("chat_not_allowed")
    return value


def common_options(document, config):
    """Flags shared by text and file sends; booleans must be real JSON booleans."""
    params = {}
    parse_mode = option(document, "parse_mode", PARSE_MODES, config["parse_mode"])
    if parse_mode != "none":
        params["parse_mode"] = parse_mode
    silent = document.get("disable_notification", config["disable_notification"])
    if type(silent) is not bool:
        fail("invalid_input", "disable_notification must be a boolean.")
    if silent:
        params["disable_notification"] = True
    reply = positive_id(document.get("reply_to_message_id"), "reply_to_message_id")
    if reply is not None:
        params["reply_parameters"] = {"message_id": reply, "allow_sending_without_reply": True}
    thread = positive_id(document.get("message_thread_id"), "message_thread_id")
    if thread is not None:
        params["message_thread_id"] = thread
    return params


def message_request(value, config):
    """Build one sendMessage call; the chat must be allowlisted before any wallet access."""
    document = structured(value, "text")
    unknown = set(document) - MESSAGE_KEYS
    if unknown:
        fail("invalid_input", "Unknown message field: " + sorted(unknown)[0] + ".")
    text = document.get("text")
    if not isinstance(text, str) or not text.strip() or "\x00" in text or len(text) > TEXT_LIMIT:
        fail("invalid_input", "text must contain 1-4096 characters and cannot be blank.")
    request_id = identifier(document.get("request_id"))
    chat = target_chat(document, config)
    params = {"chat_id": chat, "text": text, **common_options(document, config)}
    preview = document.get("disable_link_preview", config["disable_link_preview"])
    if type(preview) is not bool:
        fail("invalid_input", "disable_link_preview must be a boolean.")
    if preview:
        params["link_preview_options"] = {"is_disabled": True}
    return {"method": "sendMessage", "operation": "send_message", "chat_id": chat,
            "request_id": request_id, "params": params}


def file_request(value, config):
    """Build one file send; the path is resolved later inside file_root only."""
    document = structured(value, "path")
    unknown = set(document) - FILE_KEYS
    if unknown:
        fail("invalid_input", "Unknown file field: " + sorted(unknown)[0] + ".")
    path = document.get("path")
    if not isinstance(path, str) or not path.strip() or len(path) > 4096 or any(ord(char) < 32 for char in path):
        fail("invalid_input", "path must be a non-empty file path without control characters.")
    request_id = identifier(document.get("request_id"))
    chat = target_chat(document, config)
    caption = document.get("caption")
    if caption is not None and (not isinstance(caption, str) or len(caption) > CAPTION_LIMIT or "\x00" in caption):
        fail("invalid_input", "caption must be text of at most 1024 characters.")
    kind = document.get("as")
    if kind is not None and kind not in ("document", "photo"):
        fail("invalid_input", "as must be document or photo.")
    filename = document.get("filename")
    if filename is not None and (not isinstance(filename, str) or not 1 <= len(filename) <= 255
                                 or any(ord(char) < 32 or char in "/\\\"" for char in filename)
                                 or filename in (".", "..")):
        fail("invalid_input", "filename must be 1-255 characters without slashes, quotes or control characters.")
    params = {"chat_id": chat, **common_options(document, config)}
    if caption:
        params["caption"] = caption
    elif "parse_mode" in params:
        params.pop("parse_mode")
    return {"method": None, "operation": "send_file", "chat_id": chat, "request_id": request_id,
            "params": params, "path": path.strip(), "as": kind, "filename": filename}


def file_method(request, config, size, suffix):
    """Choose sendPhoto or sendDocument from explicit choice, mode, extension and size."""
    kind = request["as"] or config["file_mode"]
    if kind == "auto":
        kind = "photo" if suffix.lower() in PHOTO_SUFFIXES and size <= PHOTO_LIMIT else "document"
    if kind == "photo" and size > PHOTO_LIMIT:
        fail("file_too_large")
    if size > config["max_file_bytes"]:
        fail("file_too_large")
    return ("sendPhoto", "photo") if kind == "photo" else ("sendDocument", "document")


def multipart_fields(params):
    """Flatten multipart form values; nested Bot API objects are JSON-serialized strings."""
    fields = []
    for key, value in params.items():
        if isinstance(value, bool):
            fields.append([key, "true" if value else "false"])
        elif isinstance(value, (dict, list)):
            fields.append([key, json.dumps(value, separators=(",", ":"))])
        else:
            fields.append([key, str(value)])
    return fields


def map_status(status):
    return {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 409: "conflict",
            413: "payload_too_large", 429: "rate_limited"}.get(status, "provider_unavailable" if status >= 500 else "provider_error")


def provider_result(response, token=""):
    """Interpret one worker HTTP response with the documented Bot API envelope."""
    status = response.get("http_status")
    if type(status) is not int or not 100 <= status <= 599:
        fail("protocol", uncertain=True)
    body = response.get("body")
    retry_header = response.get("retry_after_header")
    if not isinstance(body, dict) or type(body.get("ok")) is not bool:
        if status >= 500:
            fail("provider_unavailable", uncertain=True, http_status=status)
        if status in (401, 404, 429):
            fail(map_status(status), http_status=status,
                 retry_after=retry_header if status == 429 and type(retry_header) is int else None)
        fail("protocol", uncertain=status < 300, http_status=status)
    if body["ok"] is True and 200 <= status < 300 and "result" in body:
        return body["result"]
    error_code = body.get("error_code")
    if type(error_code) is not int or not 100 <= error_code <= 599:
        error_code = status
    parameters = body.get("parameters") if isinstance(body.get("parameters"), dict) else {}
    retry_after = parameters.get("retry_after")
    if type(retry_after) is not int or not 0 <= retry_after <= 86400:
        retry_after = retry_header if type(retry_header) is int and 0 <= retry_header <= 86400 else None
    migrate = parameters.get("migrate_to_chat_id")
    migrate = migrate if type(migrate) is int and 1 <= abs(migrate) <= MAX_CHAT_ID else None
    description = clean(redact(body.get("description"), token), 200)
    code = map_status(error_code)
    fail(code, uncertain=code == "provider_unavailable", http_status=error_code,
         retry_after=retry_after if code == "rate_limited" else None,
         description=description, migrate_to_chat_id=migrate)


def iso(seconds):
    if type(seconds) is not int or not 0 <= seconds <= 2 ** 41:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def person(value):
    """Public profile fields only; phone numbers and other private data are never copied."""
    if not isinstance(value, dict) or type(value.get("id")) is not int:
        return None
    result = {"id": value["id"], "is_bot": value.get("is_bot") is True}
    for key, limit in (("username", 64), ("first_name", 128), ("last_name", 128), ("language_code", 16)):
        text = clean(value.get(key), limit)
        if text:
            result[key] = text
    return result


def chat(value):
    if not isinstance(value, dict) or type(value.get("id")) is not int or not isinstance(value.get("type"), str):
        return None
    result = {"id": value["id"], "type": clean(value["type"], 32)}
    for key, limit in (("title", 256), ("username", 64), ("first_name", 128), ("last_name", 128)):
        text = clean(value.get(key), limit)
        if text:
            result[key] = text
    if value.get("is_forum") is True:
        result["is_forum"] = True
    return result


def file_descriptor(kind, value):
    """Telegram file references only; contents are never downloaded by this block."""
    if not isinstance(value, dict) or not isinstance(value.get("file_id"), str):
        return None
    result = {"kind": kind, "file_id": value["file_id"][:256]}
    if isinstance(value.get("file_unique_id"), str):
        result["file_unique_id"] = value["file_unique_id"][:128]
    for key in ("file_size", "width", "height", "duration"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key, limit in (("file_name", 255), ("mime_type", 128)):
        text = clean(value.get(key), limit)
        if text:
            result[key] = text
    return result


def message_content(message):
    """Return content type, files and location from one Message without private extras."""
    files, location, kind = [], None, None
    sizes = message.get("photo")
    if isinstance(sizes, list) and sizes:
        candidates = [item for item in sizes if isinstance(item, dict)]
        if candidates:
            best = max(candidates, key=lambda item: (item.get("width", 0) if type(item.get("width")) is int else 0)
                       * (item.get("height", 0) if type(item.get("height")) is int else 0))
            descriptor = file_descriptor("photo", best)
            if descriptor:
                files.append(descriptor)
                kind = "photo"
    for name in FILE_KINDS:
        descriptor = file_descriptor(name, message.get(name))
        if descriptor:
            files.append(descriptor)
            kind = kind or name
    place = message.get("location")
    if isinstance(place, dict) and all(type(place.get(key)) in (int, float) and math.isfinite(place[key])
                                       for key in ("latitude", "longitude")):
        location = {"latitude": float(place["latitude"]), "longitude": float(place["longitude"])}
        kind = kind or "location"
    if isinstance(message.get("text"), str):
        kind = kind or "text"
    return kind or "unsupported", files, location


def normalize_update(update, config, bot_username=None):
    """Return (decision, normalized). Decisions: accepted, dropped_chat, dropped_type, invalid."""
    if not isinstance(update, dict) or type(update.get("update_id")) is not int or update["update_id"] < 0:
        return "invalid", None
    found = [name for name in UPDATE_TYPES if isinstance(update.get(name), dict)]
    if len(found) != 1 or found[0] not in config["update_types"]:
        return "dropped_type", None
    kind = found[0]
    message = update[kind]
    room = chat(message.get("chat"))
    if room is None or type(message.get("message_id")) is not int:
        return "invalid", None
    if room["id"] not in config["allowed_chat_ids"]:
        return "dropped_chat", None
    content, files, location = message_content(message)
    text = message.get("text") if isinstance(message.get("text"), str) else None
    caption = message.get("caption") if isinstance(message.get("caption"), str) else None
    command, arguments = None, None
    match = COMMAND.match(text or "")
    if match and (match.group(2) is None or (bot_username and match.group(2).lower() == bot_username.lower())):
        command, arguments = match.group(1).lower(), (text[match.end():].strip() or None)
    reply = message.get("reply_to_message")
    thread = message.get("message_thread_id")
    result = {
        "update_id": update["update_id"], "type": kind, "message_id": message["message_id"],
        "chat": room, "sender": person(message.get("from")), "sender_chat": chat(message.get("sender_chat")),
        "date": iso(message.get("date")), "date_unix": message.get("date") if type(message.get("date")) is int else None,
        "edit_date": iso(message.get("edit_date")), "content_type": content,
        "text": text[:16384] if text is not None else None,
        "caption": caption[:4096] if caption is not None else None,
        "command": command, "command_args": arguments[:4096] if arguments else None,
        "reply_to_message_id": reply.get("message_id") if isinstance(reply, dict) and type(reply.get("message_id")) is int else None,
        "message_thread_id": thread if type(thread) is int else None,
        "files": files, "location": location,
    }
    encoded(result, 262144, "protocol")
    return "accepted", result


def normalize_sent(message, request, method, attempts):
    """Public delivery receipt: provider IDs and the allowlisted chat, never the token."""
    room = chat(message.get("chat")) if isinstance(message, dict) else None
    if room is None or type(message.get("message_id")) is not int:
        fail("protocol", "Telegram accepted the request but returned an unexpected receipt; delivery is likely.",
             uncertain=True)
    result = {"request_id": request.get("request_id"), "operation": OPERATIONS[method],
              "chat_id": room["id"], "chat_type": room["type"], "message_id": message["message_id"],
              "date": iso(message.get("date")), "attempts": attempts}
    if type(message.get("message_thread_id")) is int:
        result["message_thread_id"] = message["message_thread_id"]
    if method in ("sendDocument", "sendPhoto"):
        content, files, _location = message_content(message)
        result["file"] = files[0] if files else None
        result["content_type"] = content
    return result


def usable_offset(record, now):
    """Telegram keeps updates for at most 24 hours, so an older stored offset is obsolete.

    Discarding it also avoids waiting forever when Telegram restarts update IDs
    randomly after a week without updates.
    """
    if not isinstance(record, dict):
        return None
    offset, updated = record.get("offset"), record.get("updated_at")
    if type(offset) is not int or type(updated) is not int or offset < 0:
        return None
    if not -300 <= now - updated <= OFFSET_LIFETIME_SEC:
        return None
    return offset


def state_document(value):
    """Validate the persisted per-bot offset document; malformed state is never guessed."""
    if not isinstance(value, dict) or value.get("schema_version") != 1 or set(value) != {"schema_version", "bots"}:
        fail("state_invalid")
    bots = value["bots"]
    if not isinstance(bots, dict) or len(bots) > 16:
        fail("state_invalid")
    for key, record in bots.items():
        if (not isinstance(key, str) or not re.fullmatch(r"[0-9]{1,20}", key) or not isinstance(record, dict)
                or set(record) != {"offset", "updated_at"} or type(record["offset"]) is not int
                or record["offset"] < 0 or type(record["updated_at"]) is not int or record["updated_at"] < 0):
            fail("state_invalid")
    return value


def backoff_delay(failures, code, retry_after=None):
    """Bounded exponential backoff; 429 honours retry_after up to five minutes."""
    if code == "rate_limited":
        return float(min(300, max(1, retry_after or 1)))
    if code in ("unauthorized", "not_found", "secret", "busy"):
        return 30.0
    if code == "conflict":
        return 15.0
    return float(min(60, 2 ** min(max(failures, 1) - 1, 6)))


class RateBudget:
    """Token bucket bounding published updates, so the runtime result queue stays small."""

    def __init__(self, per_minute, now):
        self.capacity = float(min(10, per_minute))
        self.rate = per_minute / 60.0
        self.tokens, self.updated = self.capacity, now

    def refill(self, now):
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self.updated) * self.rate)
        self.updated = now

    def take(self, now):
        self.refill(now)
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    def wait_time(self, now):
        self.refill(now)
        return 0.0 if self.tokens >= 1 else (1 - self.tokens) / self.rate
