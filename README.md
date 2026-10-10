# Telegram

Send messages and receive updates with a Telegram bot. The block sends text and
local files to explicitly allowed chats, and, in Active Runtime, receives new
messages by long polling the Telegram Bot API. The bot token never leaves the
BloxSmith wallet, and remote access stays off until you enable it.

<!-- block-metadata:start -->
[![Block version: 0.1.0](https://img.shields.io/badge/block-0.1.0-blue)](model.json)
[![BloxSmith compatibility: 1.0.9](https://img.shields.io/badge/BloxSmith-1.0.9-brightgreen)](compatibility.json)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Verified BloxSmith versions: **1.0.9** (bundled-block tests; see [test evidence](compatibility.json)).
<!-- block-metadata:end -->

[![Telegram — send and receive bot messages](media/thumbnail.webp)](media/cover.png)

## When to use it

- Notify a person, group or channel when a workflow finishes or needs attention.
- Send a generated report, PDF or image to a team chat.
- Build a small chat front-end: incoming messages start a workflow, replies go back.

Use one Telegram node per bot token for receiving. Messages received from chats
that are not allowlisted are dropped and counted, never published to the graph.

## Ports

| Direction | Port | Value |
| --- | --- | --- |
| Input | `message` | Plain text, or a JSON object `{chat_id, text, ...}` (see below) |
| Input | `file` | A file path inside `file_root`, or a JSON object `{path, chat_id, caption, ...}` |
| Output | `received` | One normalized update per incoming message (Active Runtime only) |
| Output | `sent` | Delivery receipt: Telegram message ID, chat ID, operation and attempts |
| Output | `error` | Machine code and English message for any failed operation |

Both inputs are optional, and each fresh value triggers exactly one operation.
A retained value is never sent again, so a replayed or forced activation does not
resend a message. Two fresh inputs in the same activation are refused with
`one_operation` and nothing is sent. Business failures are published on `error`
and the run continues; they never stop an Active Runtime worker.

### `message` input

Plain text is sent as-is to `default_chat_id`. A value whose first non-blank
character is `{` must be one JSON object with these fields (unknown fields are refused):

| Field | Meaning |
| --- | --- |
| `text` | Required, 1–4096 characters, not blank |
| `chat_id` | Numeric chat ID; defaults to `default_chat_id`; must be in `allowed_chat_ids` |
| `parse_mode` | `none`, `MarkdownV2` or `HTML`; defaults to the `parse_mode` setting |
| `disable_notification` | JSON boolean |
| `disable_link_preview` | JSON boolean |
| `reply_to_message_id` | Positive integer; sent as `reply_parameters` with `allow_sending_without_reply` |
| `message_thread_id` | Positive integer (forum topic) |
| `request_id` | Optional correlation ID (1–128 of `A-Za-z0-9._:-`), echoed in `sent` and `error` |

To send text that itself starts with `{`, wrap it: `{"text": "{literal}"}`.

### `file` input

A plain value is a path. Relative paths resolve inside `file_root`; absolute
paths must lie inside it. The JSON form accepts `path`, `chat_id`, `caption`
(at most 1024 characters), `parse_mode` (applies to the caption), `disable_notification`,
`reply_to_message_id`, `message_thread_id`, `request_id`, `as` (`document` or
`photo`) and `filename` (display name). With `file_mode=auto`, `.jpg`, `.jpeg`
and `.png` files up to 10 MiB are sent with `sendPhoto`; everything else with
`sendDocument`.

Only single-link regular files are accepted. Symbolic links (anywhere in the
path), hard links, directories, devices, FIFOs, `..` components and files outside
`file_root` are refused before any network access. The isolated network process
reopens the file component by component without following links and aborts the
upload if the file changes while it is sent.

### `received` output

```json
{
  "update_id": 812345678, "type": "message", "message_id": 42,
  "chat": {"id": 123456789, "type": "private", "first_name": "Ada"},
  "sender": {"id": 123456789, "is_bot": false, "first_name": "Ada", "username": "ada", "language_code": "fr"},
  "sender_chat": null, "date": "2026-10-10T08:00:00Z", "date_unix": 1791619200, "edit_date": null,
  "content_type": "text", "text": "/report weekly", "caption": null,
  "command": "report", "command_args": "weekly",
  "reply_to_message_id": null, "message_thread_id": null, "files": [], "location": null
}
```

`content_type` is `text`, `photo`, `document`, `voice`, `audio`, `video`,
`animation`, `video_note`, `sticker`, `location` or `unsupported`. `files`
lists Telegram file references (`file_id`, sizes, names) only; this block never
downloads file contents. Contacts, polls and other content types are reported as
`unsupported` without their data, so phone numbers are never copied into the
graph. `command` is set for `/command` or `/command@this_bot` only.

### `sent` and `error` outputs

`sent`: `{"request_id", "operation": "send_message|send_document|send_photo",
"chat_id", "chat_type", "message_id", "date", "attempts"}`, plus `file` for uploads.

`error`: `{"code", "message", "uncertain", "source": "send|receive", "operation",
"request_id", "chat_id"}` and, when Telegram provides them, `http_status`,
`retry_after`, `provider_description` (bounded, token removed) and
`migrate_to_chat_id`. `uncertain: true` means Telegram may have delivered the
message; check the chat before sending again.

| Code | Meaning |
| --- | --- |
| `disabled`, `secret`, `invalid_input`, `chat_required`, `chat_not_allowed`, `one_operation` | Refused locally before any request |
| `file_root_required`, `file_not_found`, `file_not_allowed`, `file_too_large`, `file_changed` | File policy; nothing delivered |
| `bad_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404), `conflict` (409), `payload_too_large` (413), `rate_limited` (429) | Telegram refused the request |
| `provider_unavailable` (5xx), `network`, `timeout`, `cancelled`, `protocol` | Transport outcome; `uncertain` tells whether the request may have been processed |
| `tls` | Certificate verification failed; nothing sent |
| `storage`, `state_invalid`, `busy` | Receiving cannot safely track offsets; polling stays stopped until fixed |

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Remote access switch. While off, nothing is sent or polled |
| `secret_ref` | empty | Wallet reference such as `secret://workspace/telegram_bot`, holding the BotFather token |
| `allowed_chat_ids` | `[]` | Numeric chat IDs (up to 100). Required when enabled; applies to sending and receiving |
| `default_chat_id` | empty | Chat used when an input has no `chat_id`; must be allowlisted |
| `parse_mode` | `none` | `none`, `MarkdownV2` or `HTML`. Telegram rejects malformed markup with `bad_request` |
| `file_root` | empty | Directory whose files can be sent. Empty disables the `file` input |
| `file_mode` | `auto` | `auto`, `document` or `photo` |
| `receive_updates` | `false` | Long-poll `getUpdates` in Active Runtime |
| `update_types` | `["message"]` | Any of `message`, `edited_message`, `channel_post`, `edited_channel_post` |
| `poll_timeout_sec` | 25 | Long-poll wait, 1–50 |
| `poll_limit` | 20 | Updates per `getUpdates` page, 1–100 |
| `max_updates_per_minute` | 30 | Published updates per minute, 1–120, bursts of up to 10 |
| `disable_notification`, `disable_link_preview` | `false` | Defaults for sent messages |
| `timeout_sec` | 30 | Total deadline for one send, upload and 429 retries included, 5–300 |
| `max_retries` | 2 | Retries after HTTP 429 only, 0–5 |
| `max_retry_wait_sec` | 20 | Longest `retry_after` the block will wait, 1–120 |
| `max_file_bytes` | 10485760 | Upload limit, 1 KiB–50 MiB (Telegram: 50 MiB documents, 10 MiB photos) |
| `max_response_bytes` | 1048576 | Response limit, 64 KiB–8 MiB |
| `api_base_url` | `https://api.telegram.org` | Bot API origin. Must be HTTPS; plain HTTP only for a loopback test host |
| `ca_file` | empty | Optional absolute PEM file for a privately signed HTTPS origin |

Settings are validated on Apply and again before a Run starts; invalid settings
never reach Telegram. Restart a loaded Run to use changed settings.

## Receiving messages

Receiving works only in **Active Runtime**: from Run until Stop, without Play.
One Shot sends messages but never polls. The listener calls `getMe` to identify
the bot, then `getUpdates` with `offset`, `limit`, `timeout` and
`allowed_updates`. Updates from chats outside `allowed_chat_ids`, other update
types and malformed updates are confirmed and dropped; counters (`dropped_chat`,
`dropped_type`, `dropped_invalid`, `received`, `errors`) appear in the node's
run result under `telegram.receive`.

The next offset is stored, per bot ID, in the node's private instance storage
(`telegram-updates.json`) **before** an update is handed to the runtime, so a new
Run never replays it. Delivery is therefore *at most once*: an update handed over
in the instant before Stop may not reach downstream blocks. If the runtime refuses
the hand-off, the previous offset is restored and Telegram keeps the update for the
next Run. Telegram keeps unconfirmed updates for at most 24 hours, so a stored
offset older than that is ignored. A malformed state file stops polling with
`state_invalid` instead of guessing; delete the file to accept that the last
unconfirmed batch may be delivered again.

Publication is throttled by `max_updates_per_minute`; extra messages wait on
Telegram's side. Failures are reported once per distinct code, then retried with
a bounded backoff (429 honours `retry_after`, up to five minutes). Stop interrupts
a pending long poll within about a second.

Only one poller may use a bot token: an active webhook or another poller makes
Telegram answer `409 conflict`. This block never calls `deleteWebhook`; remove a
webhook yourself if you set one.

## Example

1. Create a bot with BotFather and store its token in the wallet as `telegram_bot`.
2. Find your chat ID (for example by sending the bot a message and reading the
   `chat.id` of the `received` output), then set
   `secret_ref = secret://workspace/telegram_bot`, `allowed_chat_ids = [123456789]`,
   `default_chat_id = 123456789` and `enabled = true`.
3. Wire a Text block containing `Build finished` to `message`: One Shot sends it
   and `sent` returns `{"chat_id": 123456789, "message_id": 42, ...}`.
4. For a bot, enable `receive_updates`, wire `received` to your workflow and the
   workflow's answer back to `message` with `{"chat_id": ..., "text": ...}`.

## Limits and warnings

- Chat IDs must be numeric; `@channelusername` targets are not supported.
- Webhooks are not implemented: the block never opens a listening port.
- No HTTP proxy support; the host needs direct outbound HTTPS to the API origin.
- Received files are references only; downloading them (`getFile`) is not implemented.
- Inline keyboards, callback queries, edits and deletions of sent messages are not implemented.
- `api_base_url` other than the official origin receives the bot token: use it only
  for a trusted self-hosted Bot API server or a local test double.
- A send whose `error` reports `uncertain: true` is never retried automatically.
- Incoming chat content is untrusted data. Do not feed it to tools that execute commands.

## Dependencies

Python standard library only (`http.client`, `ssl`, `json`); no `requirements.txt`.
Each exchange runs in a short-lived isolated Python process (`python -I`), so
Cancel and Stop terminate it immediately.

## Security

- The token is read from the wallet at send or poll time and passed to the network
  process through a private pipe, never through arguments, environment, settings,
  outputs, logs, run metadata or the offset file. It is redacted from provider texts.
- TLS certificates and host names are always verified; there is no insecure mode.
- Redirects, cookies, compression and automatic retries of ambiguous sends are not used.
- Sending to a chat outside `allowed_chat_ids` is refused before the wallet is read.

## Telegram Bot API usage

Endpoints: `getMe`, `getUpdates`, `sendMessage`, `sendDocument`, `sendPhoto`
on `https://api.telegram.org/bot<token>/<method>`, with JSON bodies or
`multipart/form-data` uploads. Requests rely on `reply_parameters` and
`link_preview_options`, introduced in Bot API 7.0, and the documented
`{"ok", "result", "error_code", "description", "parameters"}` envelope.

**Real-provider qualification is pending.** All tests use a loopback double that
reproduces these documented request and response shapes; no real bot, token or
chat was contacted.

## Framing decisions

- Remote access needs `enabled`, a wallet `secret_ref` and at least one allowed chat.
- One allowlist governs both directions, so a graph cannot redirect messages to
  another chat and strangers cannot inject messages into the graph.
- Only HTTP 429 is retried (Telegram did not process the request); every other
  failure after the request was sent is reported as possibly delivered.
- At-most-once receiving with persisted per-bot offsets, as described above.
- Plain HTTP is accepted only for loopback hosts, which keeps test doubles and a
  same-host Bot API server possible without exposing the token on a network.

## Tests

`tests/F5.01_telegram.py` checks settings, input contracts, provider error
mapping, update normalization, offset storage, file policy and hooks without a
socket. `tests/F5.02_telegram_runtime.py` runs the real network process against
the loopback double (HTTP and HTTPS), including uploads, 401/403/404/409/429/5xx,
lost connections, cancellation, paging, restart without replay and prompt Stop,
then real graphs in One Shot and Active Runtime for bundled, managed and linked
packages. `tests/F8.01_telegram_ui.py` covers the English and French forms at
desktop and mobile widths. They run in the private BloxSmith workspace against a
pinned framework; the initial development version does not imply a release.
