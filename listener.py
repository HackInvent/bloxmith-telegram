"""getUpdates long polling for one Run, with persisted offsets and bounded publication.

The offset is stored before an update is handed to the runtime, so a restart
never replays it (at-most-once at the Stop boundary). If the hand-off is refused,
the previous offset is restored and Telegram keeps the update for the next Run.
All state lives in this per-hook object; nothing is kept on the block definition.
"""

from collections import deque
import time

from . import logic, storage, transport


class Halt(Exception):
    """Stop processing after a refused hand-off or storage failure."""


class UpdatePoller:
    def __init__(self, config, *, resolve_token, storage_dir, emit, stop, api=transport.call,
                 monotonic=time.monotonic, wall=time.time):
        self.config, self.resolve_token, self.storage_dir = config, resolve_token, storage_dir
        self.emit_callback, self.stop, self.api = emit, stop, api
        self.monotonic, self.wall = monotonic, wall
        self.stats = {"state": "starting", "bot_id": None, "bot_username": None, "received": 0,
                      "dropped_chat": 0, "dropped_type": 0, "dropped_invalid": 0, "errors": 0,
                      "offset": None, "last_error": None}
        self.pending = deque()
        self.token, self.bot = None, None
        self.offset, self.offset_time = None, monotonic()
        self.failures, self.retry_at, self.last_error = 0, 0.0, None
        self.budget = logic.RateBudget(config["max_updates_per_minute"], monotonic())
        self.status_due, self.dirty = 0.0, False
        self.store, self.document = None, None

    def emit(self, kind, value=None):
        self.emit_callback(kind, value, dict(self.stats))

    def wait(self, seconds):
        end = self.monotonic() + max(0.0, seconds)
        while not self.stop() and self.monotonic() < end:
            time.sleep(min(0.05, max(0.0, end - self.monotonic())))

    def report(self, error, operation):
        """Emit each distinct failure once, then back off; recovery re-arms the report."""
        self.stats.update(state="backoff", last_error=error.code, errors=self.stats["errors"] + 1)
        self.failures += 1
        self.retry_at = self.monotonic() + logic.backoff_delay(self.failures, error.code, error.retry_after)
        if error.code != self.last_error and not self.stop():
            self.last_error = error.code
            try:
                self.emit("error", error.envelope(operation=operation, source="receive"))
            except Exception:
                raise Halt() from None

    def idle(self, error, operation):
        """Fail closed: report once, then poll nothing until framework Stop."""
        try:
            self.report(error, operation)
        except Halt:
            pass
        self.stats["state"] = "blocked"
        while not self.stop():
            time.sleep(0.1)

    def run(self):
        try:
            self.store = storage.OffsetStore(self.storage_dir())
        except Exception:
            self.idle(logic.TelegramError("storage"), "get_updates")
            return
        try:
            while True:
                try:
                    acquired = self.store.acquire()
                except logic.TelegramError as exc:
                    self.idle(exc, "get_updates")
                    return
                if acquired:
                    break
                try:
                    self.report(logic.TelegramError("busy"), "get_updates")
                except Halt:
                    pass
                self.wait(2)
                if self.stop():
                    return
            try:
                self.document = self.store.load()
            except logic.TelegramError as exc:
                self.idle(exc, "get_updates")
                return
            self.loop()
        except Halt:
            while not self.stop():
                time.sleep(0.1)
        finally:
            self.store.close()

    def loop(self):
        while not self.stop():
            now = self.monotonic()
            if now < self.retry_at:
                self.wait(min(0.1, self.retry_at - now))
                continue
            if self.pending:
                self.process()
                continue
            if self.connect():
                self.fetch()
            self.flush_status()

    def connect(self):
        """Resolve the wallet token each cycle; a new token re-identifies the bot with getMe."""
        try:
            token = logic.bot_token(self.resolve_token())
        except logic.TelegramError as exc:
            self.report(exc, "get_me")
            return False
        except Exception:
            self.report(logic.TelegramError("secret"), "get_me")
            return False
        if token == self.token and self.bot is not None:
            return True
        try:
            me = self.api(self.config, token, "getMe", json_params={},
                          deadline=self.monotonic() + min(30, self.config["timeout_sec"]), cancel=self.stop)
            if not isinstance(me, dict) or type(me.get("id")) is not int or me.get("is_bot") is not True:
                logic.fail("protocol")
        except logic.TelegramError as exc:
            if not self.stop():
                self.report(exc, "get_me")
            return False
        self.token, self.bot = token, {"id": me["id"], "username": logic.clean(me.get("username"), 64)}
        self.offset = logic.usable_offset(self.document["bots"].get(str(me["id"])), int(self.wall()))
        self.offset_time = self.monotonic()
        self.stats.update(bot_id=me["id"], bot_username=self.bot["username"], offset=self.offset)
        self.recovered()
        self.flush_status(force=True)
        return True

    def recovered(self):
        self.failures, self.last_error = 0, None
        self.stats.update(state="listening")

    def fetch(self):
        if self.offset is not None and self.monotonic() - self.offset_time > logic.OFFSET_LIFETIME_SEC:
            self.offset = None
        params = {"limit": self.config["poll_limit"], "timeout": self.config["poll_timeout_sec"],
                  "allowed_updates": list(self.config["update_types"])}
        if self.offset is not None:
            params["offset"] = self.offset
        started = self.monotonic()
        try:
            updates = self.api(self.config, self.token, "getUpdates", json_params=params,
                               deadline=started + self.config["poll_timeout_sec"] + 15, cancel=self.stop)
            if not isinstance(updates, list):
                logic.fail("protocol")
        except logic.TelegramError as exc:
            if self.stop():
                return
            if exc.code in ("unauthorized", "not_found"):
                self.token = None
            self.report(exc, "get_updates")
            return
        self.recovered()
        floor = self.offset or 0
        usable = sorted((item for item in updates if isinstance(item, dict) and type(item.get("update_id")) is int
                         and item["update_id"] >= floor), key=lambda item: item["update_id"])
        invalid = len(updates) - len(usable)
        if invalid:
            self.stats["dropped_invalid"] += invalid
            self.dirty = True
        self.pending.extend(usable[:self.config["poll_limit"]])
        if not usable and self.monotonic() - started < 0.2:
            self.wait(0.2)

    def process(self):
        update = self.pending[0]
        now = self.monotonic()
        decision, normalized = logic.normalize_update(update, self.config, self.bot and self.bot["username"])
        if decision == "accepted" and not self.budget.take(now):
            self.wait(min(1.0, self.budget.wait_time(now)))
            return
        previous = self.document
        following = storage.record(previous, self.bot["id"], update["update_id"] + 1, int(self.wall()))
        try:
            self.store.save(following)
        except logic.TelegramError as exc:
            self.idle(exc, "get_updates")
            raise Halt() from None
        self.document = following
        self.stats["offset"] = update["update_id"] + 1
        if decision == "accepted":
            self.stats["received"] += 1
            try:
                self.emit("received", normalized)
            except Exception:
                # Not handed off: restore the stored offset so the next Run fetches it again.
                self.stats["received"] -= 1
                try:
                    self.store.save(previous)
                    self.document = previous
                except logic.TelegramError:
                    pass
                raise Halt() from None
        else:
            self.stats[decision] += 1
            self.dirty = True
        self.pending.popleft()
        self.offset, self.offset_time = update["update_id"] + 1, now
        self.flush_status()

    def flush_status(self, force=False):
        """Publish counters without a business output, at most once every five seconds."""
        now = self.monotonic()
        if self.stop() or not (force or (self.dirty and now >= self.status_due)):
            return
        self.dirty, self.status_due = False, now + 5
        try:
            self.emit("status")
        except Exception:
            raise Halt() from None
