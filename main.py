import json
import logging
import os
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("verfyroyal")
STOP = False
MAX_ID = 0xFFFFFFFFFF


class TelegramError(RuntimeError):
    def __init__(self, text: str, code: int | None = None, parameters: dict | None = None):
        super().__init__(text)
        self.text = text
        self.code = code
        self.parameters = parameters or {}


@dataclass(frozen=True)
class Settings:
    token: str
    owners: tuple[int, int]
    executives: tuple[int, ...]
    state_path: str
    log_level: str = "INFO"

    @property
    def configured(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*self.owners, *self.executives)))


def parse_ids(raw: str) -> tuple[int, ...]:
    values = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError("invalid Telegram user ID") from exc
        if not 1 <= value <= MAX_ID:
            raise ValueError("invalid Telegram user ID")
        values.append(value)
    return tuple(dict.fromkeys(values))


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    owners = parse_ids(env.get("VERIFICATION_OWNER_IDS", ""))
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if len(owners) != 2:
        raise ValueError("VERIFICATION_OWNER_IDS must contain exactly two distinct IDs")
    return Settings(
        token,
        (owners[0], owners[1]),
        parse_ids(env.get("VERIFICATION_EXECUTIVE_IDS", "")),
        env.get("VERIFIER_STATE_PATH", "/data/verfyroyal-events.jsonl").strip() or "/data/verfyroyal-events.jsonl",
        env.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


class Store:
    def __init__(self, path: str):
        self.path = Path(path)

    def ready(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def read(self) -> tuple[dict, ...]:
        items = []
        if not self.path.exists():
            return ()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            item = json.loads(line)
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                raise RuntimeError("invalid state record")
            items.append(item)
        return tuple(items)

    def add(self, kind: str, **data):
        record = {"type": kind, "ts": datetime.now(timezone.utc).isoformat(), **data}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def event_ids(events, kind: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(
        e["user_id"] for e in events
        if e.get("type") == kind and isinstance(e.get("user_id"), int)
    ))


def capability(events) -> str:
    state = "unknown"
    for event in events:
        if event.get("type") == "verified":
            state = "active"
        elif event.get("type") == "capability_missing":
            state = "missing"
    return state


class Telegram:
    def __init__(self, token: str, timeout=40):
        self.base = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def call(self, method: str, payload=None):
        req = urllib.request.Request(
            f"{self.base}/{method}",
            data=json.dumps(payload or {}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode())
            except Exception as parse_exc:
                raise TelegramError(f"Telegram HTTP error {exc.code}", exc.code) from parse_exc
            raise TelegramError(
                str(body.get("description", "Telegram API error")),
                body.get("error_code", exc.code),
                body.get("parameters") if isinstance(body.get("parameters"), dict) else {},
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TelegramError(f"Network error: {exc}") from exc
        try:
            body = json.loads(raw.decode())
        except Exception as exc:
            raise TelegramError("Telegram returned invalid JSON") from exc
        if not isinstance(body, dict) or body.get("ok") is not True or "result" not in body:
            raise TelegramError(
                str(body.get("description", "Telegram API request failed")) if isinstance(body, dict) else "Telegram API request failed",
                body.get("error_code") if isinstance(body, dict) and isinstance(body.get("error_code"), int) else None,
                body.get("parameters") if isinstance(body, dict) and isinstance(body.get("parameters"), dict) else {},
            )
        return body["result"]

    def get_me(self):
        result = self.call("getMe")
        if not isinstance(result, dict) or result.get("is_bot") is not True:
            raise TelegramError("invalid getMe result")
        return result

    def get_updates(self, offset):
        payload = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramError("invalid getUpdates result")
        return result

    def get_chat(self, user_id: int):
        result = self.call("getChat", {"chat_id": user_id})
        if not isinstance(result, dict) or result.get("id") != user_id:
            raise TelegramError("invalid getChat result")
        return result

    def send(self, chat_id: int, text: str, markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if markup:
            payload["reply_markup"] = markup
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("invalid sendMessage result")

    def verify(self, user_id: int):
        result = self.call("verifyUser", {"user_id": user_id})
        if result is not True:
            raise TelegramError("verifyUser did not return True", 400)
        return True


def classify(exc: TelegramError) -> str:
    text = exc.text.upper()
    if exc.code == 403 and "BOT_VERIFIER_FORBIDDEN" in text:
        return "capability_missing"
    if exc.code == 400 and ("PEER_ID_INVALID" in text or "CHAT NOT FOUND" in text):
        return "target_inaccessible"
    if exc.code == 429 or exc.code is None or exc.code >= 500:
        return "transient_api_error"
    return "verification_rejected"


def command(text: str):
    parts = text.strip().split(maxsplit=1)
    return (parts[0].split("@", 1)[0].lower(), parts[1].strip() if len(parts) == 2 else "") if parts else ("", "")


def private(message) -> bool:
    sender, chat = message.get("from") or {}, message.get("chat") or {}
    return chat.get("type") == "private" and chat.get("id") == sender.get("id")


def user_id(text: str):
    if not text or any(c.isspace() for c in text):
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if 1 <= value <= MAX_ID else None


def error(api, chat_id, kind, text, markup=None):
    api.send(chat_id, f"{kind}: {text}", markup)


def display(chat) -> str:
    name = " ".join(x.strip() for x in (chat.get("first_name", ""), chat.get("last_name", "")) if isinstance(x, str) and x.strip())
    username = chat.get("username") if isinstance(chat.get("username"), str) else ""
    return f"{name or chat['id']} (@{username})" if username else name or str(chat["id"])


def share_button(bot_username: str):
    link = f"https://t.me/{bot_username}"
    text = "Abra este bot e envie /start para participar da verificação institucional."
    url = "https://t.me/share/url?url=" + urllib.parse.quote(link, safe="") + "&text=" + urllib.parse.quote(text, safe="")
    return {"inline_keyboard": [[{"text": "Enviar instrução ao alvo", "url": url}]]}


def confirm_button(owner_id: int, target_id: int):
    return {"inline_keyboard": [[
        {"text": "Confirmar", "callback_data": f"verify:{owner_id}:{target_id}"},
        {"text": "Cancelar", "callback_data": f"cancel:{owner_id}:{target_id}"},
    ]]}


def owner_status(api, events, owner_id):
    if owner_id in event_ids(events, "verified"):
        return "verified"
    try:
        api.get_chat(owner_id)
        return "unverified"
    except TelegramError as exc:
        return "inaccessible" if classify(exc) == "target_inaccessible" else "error"


def show_start(api, store, settings, owner_id):
    events = store.read()
    cap, status = capability(events), owner_status(api, events, owner_id)
    if cap == "missing":
        api.send(owner_id, f"Estado A\ncapability: missing\nowner: {status}\nUse /verifyme quando a permissão estiver disponível.")
        return
    if status != "verified":
        api.send(owner_id, f"Estado B\ncapability: {cap}\nowner: {status}\nUse /verifyme para verificar sua conta.")
        return
    verified = event_ids(events, "verified")
    lines = ["Estado C", "Verificados:"]
    for uid in verified:
        try:
            lines.append("• " + display(api.get_chat(uid)))
        except TelegramError:
            pass
    lines += [f"Pendentes: {sum(uid not in verified for uid in settings.configured)}", "Use /verify <user_id> para verificar uma conta."]
    api.send(owner_id, "\n".join(lines))


def verify_owner(api, store, owner_id):
    if owner_id in event_ids(store.read(), "verified"):
        api.send(owner_id, "Sua conta já está verificada.")
        return
    try:
        api.verify(owner_id)
    except TelegramError as exc:
        kind = classify(exc)
        if kind == "capability_missing":
            store.add("capability_missing", owner_id=owner_id)
        error(api, owner_id, kind, "Sua conta não foi verificada.")
        return
    store.add("verified", user_id=owner_id, owner_id=owner_id)
    api.send(owner_id, "Sua conta foi verificada com sucesso.")


def prepare_target(api, store, settings, owner_id, raw_target, bot_username):
    events, target = store.read(), user_id(raw_target)
    cap, verified, started = capability(events), event_ids(events, "verified"), event_ids(events, "start")
    if cap != "active":
        error(api, owner_id, "capability_missing" if cap == "missing" else "capability_unknown", "Verifique primeiro a sua própria conta.")
        return
    if owner_id not in verified:
        error(api, owner_id, "owner_not_verified", "Verifique primeiro a sua própria conta.")
        return
    if target is None or target in settings.owners:
        error(api, owner_id, "invalid_target_id", "Use /verify <user_id> com um ID de terceiro válido.")
        return
    if target in verified:
        error(api, owner_id, "already_verified", "Essa conta já está verificada.")
        return
    if target not in started:
        error(api, owner_id, "target_inaccessible", "O alvo precisa enviar /start antes.", share_button(bot_username))
        return
    try:
        chat = api.get_chat(target)
    except TelegramError as exc:
        kind = classify(exc)
        error(api, owner_id, kind if kind != "verification_rejected" else "target_resolution_error", "Não foi possível validar o alvo.", share_button(bot_username) if kind == "target_inaccessible" else None)
        return
    api.send(owner_id, f"Confirmar verificação de {display(chat)}\nID: {target}", confirm_button(owner_id, target))


def verify_target(api, store, settings, owner_id, target, bot_username):
    events = store.read()
    verified, started = event_ids(events, "verified"), event_ids(events, "start")
    if capability(events) != "active":
        error(api, owner_id, "capability_missing", "A verificação não pode ser executada agora.")
        return
    if owner_id not in verified:
        error(api, owner_id, "owner_not_verified", "Sua conta de owner não está verificada.")
        return
    if target in settings.owners or target in verified:
        error(api, owner_id, "invalid_target_id" if target in settings.owners else "already_verified", "O alvo não pode ser verificado por este fluxo.")
        return
    if target not in started:
        error(api, owner_id, "target_inaccessible", "O alvo precisa enviar /start antes.", share_button(bot_username))
        return
    try:
        api.get_chat(target)
        api.verify(target)
    except TelegramError as exc:
        kind = classify(exc)
        if kind == "capability_missing":
            store.add("capability_missing", owner_id=owner_id)
        error(api, owner_id, kind, "A conta não foi verificada.", share_button(bot_username) if kind == "target_inaccessible" else None)
        return
    store.add("verified", user_id=target, owner_id=owner_id)
    api.send(owner_id, f"Conta {target} verificada com sucesso.")


def handle_message(api, store, settings, message, bot_username):
    text, sender, chat = message.get("text"), message.get("from") or {}, message.get("chat") or {}
    sender_id, chat_id = sender.get("id"), chat.get("id")
    if not isinstance(text, str) or not isinstance(sender_id, int) or not isinstance(chat_id, int):
        return
    cmd, arg = command(text)
    if cmd not in {"/start", "/verifyme", "/verify"}:
        return
    if cmd == "/start" and private(message):
        if sender_id not in event_ids(store.read(), "start"):
            store.add("start", user_id=sender_id)
        show_start(api, store, settings, sender_id) if sender_id in settings.owners else api.send(chat_id, "Conversa iniciada. Um owner pode agora solicitar sua verificação.")
        return
    if sender_id not in settings.owners:
        error(api, chat_id, "unauthorized_sender", "Somente owners autorizam verificações.")
        return
    if not private(message):
        error(api, chat_id, "non_private_chat", "Abra a conversa privada com o bot.")
        return
    verify_owner(api, store, sender_id) if cmd == "/verifyme" else prepare_target(api, store, settings, sender_id, arg, bot_username)


def handle_callback(api, store, settings, query, bot_username):
    sender, message = query.get("from") or {}, query.get("message") or {}
    data, sender_id, chat = query.get("data"), sender.get("id"), message.get("chat") or {}
    chat_id = chat.get("id")
    if not isinstance(data, str) or not isinstance(sender_id, int) or not isinstance(chat_id, int):
        return
    if sender_id not in settings.owners or chat.get("type") != "private" or chat_id != sender_id:
        error(api, chat_id, "unauthorized_sender", "Confirmação não autorizada.")
        return
    parts = data.split(":")
    expected = user_id(parts[1]) if len(parts) == 3 else None
    target = user_id(parts[2]) if len(parts) == 3 else None
    if len(parts) != 3 or parts[0] not in {"verify", "cancel"} or target is None:
        error(api, chat_id, "verification_rejected", "Confirmação inválida.")
        return
    if expected != sender_id:
        error(api, chat_id, "unauthorized_sender", "Confirmação inválida para este owner.")
        return
    if parts[0] == "cancel":
        api.send(chat_id, "Verificação cancelada.")
        return
    verify_target(api, store, settings, sender_id, target, bot_username)


def prepare(api):
    me = api.get_me()
    username = me.get("username")
    if not isinstance(username, str) or not username:
        raise TelegramError("bot username is required")
    if api.call("deleteWebhook", {"drop_pending_updates": False}) is not True:
        raise TelegramError("deleteWebhook failed")
    return username


def run(api, store, settings, bot_username):
    offset = None
    while not STOP:
        try:
            for update in api.get_updates(offset):
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    raise TelegramError("invalid update_id")
                offset = update_id + 1
                if isinstance(update.get("message"), dict):
                    handle_message(api, store, settings, update["message"], bot_username)
                if isinstance(update.get("callback_query"), dict):
                    handle_callback(api, store, settings, update["callback_query"], bot_username)
        except Exception:
            LOG.exception("worker error")
            time.sleep(2)


def stop(_signum, _frame):
    global STOP
    STOP = True


def main():
    settings = load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    api, store = Telegram(settings.token), Store(settings.state_path)
    store.ready()
    run(api, store, settings, prepare(api))


if __name__ == "__main__":
    main()
