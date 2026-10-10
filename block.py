"""Telegram bot messages through wallet-held tokens, owned child processes and Run listeners."""

import json
from bloxsmith_app.block_api import APPLICATION_JSON, BlockDefinition, BlockRuntimeOutput, BlockRuntimePreparation, BlockRuntimeResult
from . import listener, logic, transport, ui


# FB1 - Keep remote access off by default; require a wallet bot token, an HTTPS API origin and allowlisted numeric chats.
# FB2 - Send text with sendMessage (parse_mode none|MarkdownV2|HTML) and bounded file_root files with sendDocument/sendPhoto.
# FB3 - Bound size, time and retries; honour 429 retry_after, never replay ambiguous sends, report coded English errors without the token.
# FB4 - Long-poll getUpdates only in Active Runtime, drop other chats with counters, persist offsets so restarts never replay, stop promptly.
# FB5 - Apply one contract in both runtimes and every package origin; One Shot sends but never polls.
# FB6 - Own translated responsive settings with draft Apply, conditional sections and release-scoped assets.
class TelegramBlock(BlockDefinition):
    """Incoming chat content is untrusted data, never instructions for this block."""

    kind = "telegram"

    def ui_assets(self, surface="modal"):
        """Every installation uses the same static release manifest."""
        return list(self.model.get("ui_assets", {}).get(surface, []))

    def prepare_runtime(self, context):
        """Validate settings without wallet, network or storage access; declare the poller."""
        config = logic.configuration(context.config)
        return BlockRuntimePreparation(listen_on_run=context.runtime_mode == "zeromq_active"
                                       and config["enabled"] and config["receive_updates"])

    def outputs(self, context, values):
        return [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, content_type=APPLICATION_JSON,
                                   value=json.dumps(values[port.name], ensure_ascii=False, allow_nan=False))
                for port in context.output_ports if port.name in values]

    def fresh_inputs(self, context):
        """Only fresh deliveries count: retained values never resend a message."""
        names = {int(port.id): str(port.name) for port in context.input_ports}
        if context.input_events:
            return [(names.get(event.input_port_id), event.value) for event in context.input_events]
        fresh = []
        for ident, name in names.items():
            attribute = context.input_attribute(name, str(ident))
            if attribute is not None and attribute.status == "updated" and attribute.value not in (None, ""):
                fresh.append((name, attribute.value))
        return fresh

    def execute_runtime(self, context):
        """One fresh message or file per activation; business failures go to the error port."""
        request, token = {}, ""
        try:
            config = logic.configuration(context.config)
            items = self.fresh_inputs(context)
            if not items:
                return BlockRuntimeResult(status="skipped", outputs=[])
            if len(items) != 1:
                logic.fail("one_operation")
            name, value = items[0]
            if name not in ("message", "file"):
                logic.fail("invalid_input", "Unknown input port.")
            if not config["enabled"]:
                logic.fail("disabled")
            request = logic.message_request(value, config) if name == "message" else logic.file_request(value, config)
            located = transport.locate(request["path"], config) if name == "file" else None
            if located is not None:
                logic.file_method(request, config, located["size"], located["suffix"])
            try:
                token = logic.bot_token(context.resolve_secret(config["secret_ref"]))
            except logic.TelegramError:
                raise
            except Exception:
                logic.fail("secret")
            method, sent = transport.send(config, token, request,
                                          context.services.get("cancel_requested", lambda: False), located)
            return BlockRuntimeResult(status="success", outputs=self.outputs(context, {"sent": sent}),
                content_type=APPLICATION_JSON, last_message=logic.OPERATIONS[method],
                metadata={self.kind: {"send": {"status": "sent", "operation": logic.OPERATIONS[method],
                                               "attempts": sent["attempts"]}}},
                logs=[f"[telegram] {method} delivered after {sent['attempts']} attempt(s)."])
        except logic.TelegramError as exc:
            error = exc
        except (ValueError, TypeError, OSError, RuntimeError):
            # Raw exception text could carry paths or provider data: publish a fixed diagnostic.
            error = logic.TelegramError("internal", uncertain=bool(token))
        operation = request.get("operation")
        envelope = error.envelope(operation=operation, request_id=request.get("request_id"),
                                  chat_id=request.get("chat_id"), source="send")
        text = context.redact_secrets(logic.redact(json.dumps(envelope, ensure_ascii=False, allow_nan=False), token))
        return BlockRuntimeResult(status="success",
            outputs=[BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, value=text,
                                        content_type=APPLICATION_JSON)
                     for port in context.output_ports if port.name == "error"],
            content_type=APPLICATION_JSON, last_message=error.code,
            metadata={self.kind: {"send": {"status": "error", "code": error.code, "uncertain": error.uncertain}}},
            logs=[f"[telegram-error] {operation or 'input'}: {error.code}"])

    def listen_runtime(self, context):
        """Poll getUpdates until framework Stop; listener state is local to this call."""
        config = logic.configuration(context.config)
        services = context.services

        def resolve():
            resolver = services.get("resolve_secret")
            if not callable(resolver):
                logic.fail("secret")
            return resolver(config["secret_ref"])

        def storage_dir():
            provider = services.get("get_block_storage_dir")
            if not callable(provider):
                logic.fail("storage")
            return provider()

        def emit(kind, value, stats):
            if context.stop_requested():
                raise RuntimeError("Listener stopped.")
            values = {kind: value} if kind in ("received", "error") else {}
            text = None
            if kind == "error":
                redactor = services.get("redact_secrets")
                text = json.dumps(value, ensure_ascii=False, allow_nan=False)
                text = str(redactor(text)) if callable(redactor) else text
            outputs = [BlockRuntimeOutput(port_id=int(port.id), port_name=port.name, content_type=APPLICATION_JSON,
                                          value=text if kind == "error" else json.dumps(value, ensure_ascii=False, allow_nan=False))
                       for port in context.output_ports if port.name in values]
            logs = [f"[telegram-error] {value.get('operation', 'get_updates')}: {value.get('code')}"] if kind == "error" else []
            context.emit_result(BlockRuntimeResult(status="success", outputs=outputs, content_type=APPLICATION_JSON,
                                                   metadata={self.kind: {"receive": stats}}, logs=logs))

        listener.UpdatePoller(config, resolve_token=resolve, storage_dir=storage_dir, emit=emit,
                              stop=context.stop_requested).run()

    def handle_ui_action(self, *, node, action, values, payload=None):
        """Apply validates settings only; it never contacts Telegram or reads the wallet."""
        if action in {"save_settings", "modal_update_fields", "inspector_update_fields"}:
            patch = (values or {}).get("node_patch") or {}
            if "config" in patch:
                try:
                    logic.configuration({**self.default_config(), **(node.get("config") or {}), **patch["config"]})
                except (ValueError, TypeError) as exc:
                    detail = str(exc) if isinstance(exc, logic.TelegramError) else logic.MESSAGES["config"]
                    return {"error": self.translate("block.telegram.error", {"detail": detail},
                                                    fallback="Telegram settings rejected: " + detail)}
        result = super().handle_ui_action(node=node, action="modal_update_fields" if action == "save_settings" else action,
                                          values=values, payload=payload)
        return ui.replace_config(result, node)

    def render_modal(self, *, node, payload=None):
        return ui.modal(self, node, payload)

    def render_inspector_panel(self, *, node, payload=None):
        return ui.inspector(self, node, payload)

    def render_node_card(self, *, node, payload=None):
        """Show the access mode and allowlist size, never the token reference or chat IDs."""
        config = {**self.default_config(), **(node.get("config") or {})}
        chats = config.get("allowed_chat_ids")
        count = len(chats) if isinstance(chats, list) else 0
        # The key and its parameters travel with the text so the editor re-renders it in the
        # interface language, even when a package process rendered it with the English pivot.
        if config.get("enabled") is not True:
            key, params, fallback = "block.telegram.card_disabled", {}, "Remote access off"
        elif config.get("receive_updates") is True:
            key, params, fallback = "block.telegram.card_receive", {"count": count}, f"Send and receive · {count} chats"
        else:
            key, params, fallback = "block.telegram.card_send", {"count": count}, f"Send only · {count} chats"
        return ui.card(self, node, key, params, self.translate(key, params, fallback=fallback))
