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
from typing import Callable, Literal


LOGGER = logging.getLogger("verfyroyal")
_STOP = False
MAX_TELEGRAM_USER_ID = 0xFFFFFFFFFF
DEFAULT_ATTEMPTS = 3

CapabilityState = Literal["active", "missing", "unknown"]
OwnerStatus = Literal["verified", "unverified", "inaccessible", "error"]


class TelegramAPIError(RuntimeError):
    def __init__(self, description: str, error_code: int | None = None, parameters: dict | None = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.parameters = parameters or {}


class StateStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    owner_ids: tuple[int, int]
    executive_ids: tuple[int, ...]
    state_path: str
    log_level: str = "INFO"

    @property
    def configured_targets(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*self.owner_ids, *self.executive_ids)))


@dataclass(frozen=True)
class ResolvedUser:
    user_id: int
    name: str
    username: str | None


@dataclass(frozen=True)
class Inventory:
    verified: tuple[ResolvedUser, ...]
    verified_unresolved_count: int
    pending_count: int


def _parse_ids(raw: str, name: str, required: bool) -> tuple[int, ...]:
    raw = raw.strip()
    if not raw:
        if required:
            raise ValueError(f"{name} is required")
        return ()

    values: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            value = int(item)
        except ValueError as exc:
            raise ValueError(f"{name} must contain comma-separated Telegram user IDs") from exc
        if not 1 <= value <= MAX_TELEGRAM_USER_ID:
            raise ValueError(f"{name} contains an invalid Telegram user ID")
        values.append(value)

    values = list(dict.fromkeys(values))
    if required and not values:
        raise ValueError(f"{name} is required")
    return tuple(values)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    source = os.environ if env is None else env
    token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")

    owners = _parse_ids(source.get("VERIFICATION_OWNER_IDS", ""), "VERIFICATION_OWNER_IDS", True)
    if len(owners) != 2:
        raise ValueError("VERIFICATION_OWNER_IDS must contain exactly two distinct owner IDs")

    executives = _parse_ids(
        source.get("VERIFICATION_EXECUTIVE_IDS", ""),
        "VERIFICATION_EXECUTIVE_IDS",
        False,
    )
    state_path = source.get("VERIFIER_STATE_PATH", "/data/verfyroyal-events.jsonl").strip()
    if not state_path:
        raise ValueError("VERIFIER_STATE_PATH must not be empty")
    level = source.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    return Settings(token, (owners[0], owners[1]), executives, state_path, level)


class EventStore:
    """Append-only runtime evidence. It never writes inferred verification state."""

    def __init__(self, path: str):
        self.path = Path(path)

    def read_events(self) -> tuple[dict, ...]:
        if not self.path.exists():
            return ()
        events: list[dict] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise StateStoreError(f"invalid state event at line {line_number}") from exc
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise StateStoreError(f"invalid state event at line {line_number}")
                    events.append(event)
        except OSError as exc:
            raise StateStoreError(f"unable to read state store: {exc}") from exc
        return tuple(events)

    def append(self, event: dict) -> None:
        record = {
            "v": 1,
            "ts": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise StateStoreError(f"unable to append state event: {exc}") from exc

    def record_verification_success(self, subject_id: int, actor_id: int, mode: str) -> None:
        self.append({
            "type": "verification_succeeded",
            "subject_id": subject_id,
            "actor_id": actor_id,
            "mode": mode,
        })

    def record_capability_missing(self, actor_id: int) -> None:
        self.append({"type": "capability_missing", "actor_id": actor_id})


# Pure diagnostic reducers: they inspect evidence but never call Telegram or mutate state.
def capability_from_events(events: tuple[dict, ...]) -> CapabilityState:
    state: CapabilityState = "unknown"
    for event in events:
        if event.get("type") == "verification_succeeded":
            state = "active"
        elif event.get("type") == "capability_missing":
            state = "missing"
    return state


def verified_ids_from_events(events: tuple[dict, ...]) -> tuple[int, ...]:
    ids: list[int] = []
    for event in events:
        if event.get("type") != "verification_succeeded":
            continue
        subject_id = event.get("subject_id")
        if isinstance(subject_id, int) and 1 <= subject_id <= MAX_TELEGRAM_USER_ID:
            ids.append(subject_id)
    return tuple(dict.fromkeys(ids))


def owner_status_from_evidence(owner_id: int, verified_ids: tuple[int, ...], resolution: str) -> OwnerStatus:
    if owner_id in verified_ids:
        return "verified"
    if resolution == "ok":
        return "unverified"
    if resolution == "inaccessible":
        return "inaccessible"
    return "error"


class TelegramBotAPI:
    def __init__(self, token: str, timeout: int = 40):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def call(self, method: str, payload: dict | None = None) -> object:
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as parse_exc:
                raise TelegramAPIError(f"Telegram HTTP error {exc.code}", exc.code) from parse_exc
            if not isinstance(body, dict):
                raise TelegramAPIError(f"Telegram HTTP error {exc.code}", exc.code)
            raise TelegramAPIError(
                str(body.get("description", f"Telegram HTTP error {exc.code}")),
                body.get("error_code") if isinstance(body.get("error_code"), int) else exc.code,
                body.get("parameters") if isinstance(body.get("parameters"), dict) else {},
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise TelegramAPIError(f"Network error contacting Telegram: {reason}") from exc

        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramAPIError("Telegram returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise TelegramAPIError("Telegram returned an invalid response")
        if body.get("ok") is not True:
            raise TelegramAPIError(
                str(body.get("description", "Telegram API request failed")),
                body.get("error_code") if isinstance(body.get("error_code"), int) else None,
                body.get("parameters") if isinstance(body.get("parameters"), dict) else {},
            )
        if "result" not in body:
            raise TelegramAPIError("Telegram success response did not contain result")
        return body["result"]

    def get_me(self) -> dict:
        result = self.call("getMe")
        if not isinstance(result, dict) or result.get("is_bot") is not True or not isinstance(result.get("id"), int):
            raise TelegramAPIError("getMe did not return a valid bot identity")
        return result

    def delete_webhook(self) -> None:
        if self.call("deleteWebhook", {"drop_pending_updates": False}) is not True:
            raise TelegramAPIError("deleteWebhook did not return True")

    def get_updates(self, offset: int | None) -> list[dict]:
        payload: dict[str, object] = {
            "timeout": 30,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        if not isinstance(result, list) or not all(isinstance(update, dict) for update in result):
            raise TelegramAPIError("getUpdates returned an invalid result")
        return result

    def get_chat(self, user_id: int) -> dict:
        result = self.call("getChat", {"chat_id": user_id})
        if not isinstance(result, dict) or result.get("id") != user_id:
            raise TelegramAPIError("getChat returned an unexpected identity")
        return result

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self.call("sendMessage", payload)
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramAPIError("sendMessage did not return a valid Message")

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload: dict[str, object] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if self.call("answerCallbackQuery", payload) is not True:
            raise TelegramAPIError("answerCallbackQuery did not return True")

    def verify_user(self, user_id: int) -> bool:
        result = self.call("verifyUser", {"user_id": user_id})
        if result is not True:
            raise TelegramAPIError("verifyUser did not return True", 400)
        return True


def _command_parts(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped:
        return "", ""
    pieces = stripped.split(maxsplit=1)
    command = pieces[0].split("@", 1)[0].lower()
    argument = pieces[1].strip() if len(pieces) == 2 else ""
    return command, argument


def _is_private_1to1(message: dict) -> bool:
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    return chat.get("type") == "private" and chat.get("id") == sender.get("id")


def _is_permission_missing(exc: TelegramAPIError) -> bool:
    return exc.error_code == 403 and "BOT_VERIFIER_FORBIDDEN" in exc.description.upper()


def _is_inaccessible_peer(exc: TelegramAPIError) -> bool:
    upper = exc.description.upper()
    return exc.error_code == 400 and ("PEER_ID_INVALID" in upper or "CHAT NOT FOUND" in upper)


def _is_transient(exc: TelegramAPIError) -> bool:
    return exc.error_code == 429 or exc.error_code is None or (exc.error_code is not None and exc.error_code >= 500)


def _retry_delay(exc: TelegramAPIError, attempt: int) -> int | None:
    if exc.error_code == 429:
        retry_after = exc.parameters.get("retry_after")
        return retry_after if isinstance(retry_after, int) and retry_after > 0 else 1
    if exc.error_code is None or (exc.error_code is not None and exc.error_code >= 500):
        return 2 ** attempt
    return None


def _with_transient_retry(operation: Callable[[], None], attempts: int = DEFAULT_ATTEMPTS) -> None:
    for attempt in range(attempts):
        try:
            operation()
            return
        except TelegramAPIError as exc:
            delay = _retry_delay(exc, attempt)
            if delay is None or attempt == attempts - 1:
                raise
            time.sleep(delay)


def _resolution_state(api: TelegramBotAPI, user_id: int) -> tuple[str, dict | None]:
    try:
        return "ok", api.get_chat(user_id)
    except TelegramAPIError as exc:
        if _is_inaccessible_peer(exc):
            return "inaccessible", None
        return "error", None


def diagnose_capability(store: EventStore) -> CapabilityState:
    return capability_from_events(store.read_events())


def diagnose_owner_status(api: TelegramBotAPI, store: EventStore, owner_id: int) -> OwnerStatus:
    events = store.read_events()
    verified = verified_ids_from_events(events)
    if owner_id in verified:
        return "verified"
    resolution, _ = _resolution_state(api, owner_id)
    return owner_status_from_evidence(owner_id, verified, resolution)


def _display_name(chat: dict) -> tuple[str, str | None] | None:
    first = chat.get("first_name")
    last = chat.get("last_name")
    username = chat.get("username")
    if not isinstance(first, str) or not first.strip():
        return None
    name = first.strip()
    if isinstance(last, str) and last.strip():
        name += " " + last.strip()
    return name, username if isinstance(username, str) and username else None


def build_inventory(api: TelegramBotAPI, store: EventStore, settings: Settings) -> Inventory:
    verified_ids = verified_ids_from_events(store.read_events())
    resolved: list[ResolvedUser] = []
    unresolved = 0
    for user_id in verified_ids:
        resolution, chat = _resolution_state(api, user_id)
        if resolution != "ok" or chat is None:
            unresolved += 1
            continue
        identity = _display_name(chat)
        if identity is None:
            unresolved += 1
            continue
        name, username = identity
        resolved.append(ResolvedUser(user_id, name, username))

    verified_set = set(verified_ids)
    pending = sum(1 for user_id in settings.configured_targets if user_id not in verified_set)
    return Inventory(tuple(resolved), unresolved, pending)


def _private_bot_url(bot_username: str) -> str:
    return f"https://t.me/{bot_username}?start=verify"


def _open_private_keyboard(bot_username: str) -> dict:
    return {"inline_keyboard": [[{"text": "Abrir conversa privada", "url": _private_bot_url(bot_username)}]]}


def _self_verify_keyboard() -> dict:
    return {"inline_keyboard": [[{"text": "Verificar minha conta", "callback_data": "owner:self"}]]}


def _confirmation_keyboard(user_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "Confirmar verificação", "callback_data": f"verify:{user_id}"},
        {"text": "Cancelar", "callback_data": f"cancel:{user_id}"},
    ]]}


def _share_instruction_keyboard(bot_username: str) -> dict:
    bot_url = _private_bot_url(bot_username)
    text = "Para sua verificação institucional, abra o bot e envie /start para iniciar a conversa."
    share_url = (
        "https://t.me/share/url?url="
        + urllib.parse.quote(bot_url, safe="")
        + "&text="
        + urllib.parse.quote(text, safe="")
    )
    return {"inline_keyboard": [[{"text": "Enviar instrução ao alvo", "url": share_url}]]}


def _send(api: TelegramBotAPI, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
    _with_transient_retry(lambda: api.send_message(chat_id, text, reply_markup))


def _send_error(
    api: TelegramBotAPI,
    chat_id: int,
    error_class: str,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    _send(api, chat_id, f"Erro [{error_class}]: {text}", reply_markup)


def _record_api_outcome(store: EventStore, actor_id: int, exc: TelegramAPIError | None) -> None:
    if exc is not None and _is_permission_missing(exc):
        store.record_capability_missing(actor_id)


def _classify_verification_error(exc: TelegramAPIError) -> str:
    if _is_permission_missing(exc):
        return "capability_missing"
    if _is_inaccessible_peer(exc):
        return "target_inaccessible"
    if _is_transient(exc):
        return "transient_api_error"
    return "verification_rejected"


def _owner_start_state(
    api: TelegramBotAPI,
    store: EventStore,
    settings: Settings,
    owner_id: int,
) -> tuple[str, CapabilityState, OwnerStatus, Inventory | None]:
    capability = diagnose_capability(store)
    owner_status = diagnose_owner_status(api, store, owner_id)
    if capability == "missing":
        return "A", capability, owner_status, None
    if owner_status == "verified":
        return "C", capability, owner_status, build_inventory(api, store, settings)
    return "B", capability, owner_status, None


def _render_owner_start(
    api: TelegramBotAPI,
    store: EventStore,
    settings: Settings,
    owner_id: int,
    chat_id: int,
) -> None:
    state, capability, owner_status, inventory = _owner_start_state(api, store, settings, owner_id)
    if state == "A":
        _send(
            api,
            chat_id,
            (
                "Estado A\n"
                f"Capacidade do bot: {capability}\n"
                f"Status do owner: {owner_status}\n"
                "A capacidade de verificador foi observada como indisponível. "
                "Quando o Telegram conceder a capacidade, use a ação abaixo para validar sua própria conta."
            ),
            _self_verify_keyboard(),
        )
        return

    if state == "B":
        markup = _self_verify_keyboard() if owner_status == "unverified" else None
        _send(
            api,
            chat_id,
            (
                "Estado B\n"
                f"Capacidade do bot: {capability}\n"
                f"Status do owner: {owner_status}\n"
                "Nenhuma verificação de terceiro foi executada. "
                "A próxima ação é verificar somente a sua própria conta."
            ),
            markup,
        )
        return

    assert inventory is not None
    lines = [
        "Estado C",
        f"Capacidade do bot: {capability}",
        f"Status do owner: {owner_status}",
        "Verificados:",
    ]
    if inventory.verified:
        for item in inventory.verified:
            username = f" (@{item.username})" if item.username else ""
            lines.append(f"• {item.name}{username}")
    else:
        lines.append("• nenhum nome resolvível no momento")
    if inventory.verified_unresolved_count:
        lines.append(f"Verificados sem identidade resolvível agora: {inventory.verified_unresolved_count}")
    lines.append(f"Pendentes configurados: {inventory.pending_count}")
    lines.append("Próxima ação: /verify <user_id> para preparar uma verificação unitária de terceiro.")
    _send(api, chat_id, "\n".join(lines))


def execute_owner_self_verification(
    api: TelegramBotAPI,
    store: EventStore,
    owner_id: int,
    chat_id: int,
) -> None:
    try:
        result = api.verify_user(owner_id)
    except TelegramAPIError as exc:
        _record_api_outcome(store, owner_id, exc)
        error_class = _classify_verification_error(exc)
        _send_error(api, chat_id, error_class, "A auto-verificação do owner não foi concluída.")
        return

    if result is not True:
        _send_error(api, chat_id, "verification_rejected", "A auto-verificação não retornou True.")
        return
    store.record_verification_success(owner_id, owner_id, "owner_self")
    _send(api, chat_id, "Sucesso: sua conta de owner foi verificada e o resultado True foi persistido.")


def _parse_target_id(argument: str) -> int | None:
    if not argument or any(char.isspace() for char in argument):
        return None
    try:
        user_id = int(argument)
    except ValueError:
        return None
    return user_id if 1 <= user_id <= MAX_TELEGRAM_USER_ID else None


def prepare_third_party_confirmation(
    api: TelegramBotAPI,
    store: EventStore,
    settings: Settings,
    owner_id: int,
    chat_id: int,
    argument: str,
    bot_username: str,
) -> None:
    capability = diagnose_capability(store)
    if capability == "missing":
        _send_error(api, chat_id, "capability_missing", "A capacidade de verificador está indisponível.")
        return
    if capability != "active":
        _send_error(
            api,
            chat_id,
            "capability_unknown",
            "A capacidade ainda não foi confirmada. Use /verifyme para validar o owner primeiro.",
        )
        return

    owner_status = diagnose_owner_status(api, store, owner_id)
    if owner_status != "verified":
        _send_error(api, chat_id, "owner_not_verified", "O owner precisa estar verificado antes de autorizar terceiros.")
        return

    target_id = _parse_target_id(argument)
    if target_id is None:
        _send_error(api, chat_id, "invalid_target_id", "Use /verify <user_id> com um único ID válido.")
        return

    resolution, chat = _resolution_state(api, target_id)
    if resolution == "inaccessible":
        _send_error(
            api,
            chat_id,
            "target_inaccessible",
            "O alvo precisa abrir a conversa com o bot e enviar /start antes da verificação.",
            _share_instruction_keyboard(bot_username),
        )
        return
    if resolution == "error":
        _send_error(api, chat_id, "transient_api_error", "Não foi possível resolver o alvo agora.")
        return

    identity = _display_name(chat or {})
    if identity is None:
        target_label = f"ID {target_id}"
    else:
        name, username = identity
        target_label = f"{name} (@{username})" if username else name

    _send(
        api,
        chat_id,
        (
            "Confirme o alvo da verificação unitária:\n"
            f"{target_label}\n"
            f"ID: {target_id}\n"
            "Nenhuma verificação foi executada ainda."
        ),
        _confirmation_keyboard(target_id),
    )


def execute_confirmed_third_party(
    api: TelegramBotAPI,
    store: EventStore,
    settings: Settings,
    owner_id: int,
    chat_id: int,
    target_id: int,
    bot_username: str,
) -> None:
    capability = diagnose_capability(store)
    if capability == "missing":
        _send_error(api, chat_id, "capability_missing", "A capacidade de verificador está indisponível.")
        return
    if capability != "active":
        _send_error(api, chat_id, "capability_unknown", "A capacidade do bot não está confirmada como active.")
        return

    if diagnose_owner_status(api, store, owner_id) != "verified":
        _send_error(api, chat_id, "owner_not_verified", "O owner não está registrado como verificado.")
        return

    resolution, _ = _resolution_state(api, target_id)
    if resolution == "inaccessible":
        _send_error(
            api,
            chat_id,
            "target_inaccessible",
            "O alvo precisa abrir a conversa com o bot e enviar /start antes da verificação.",
            _share_instruction_keyboard(bot_username),
        )
        return
    if resolution == "error":
        _send_error(api, chat_id, "transient_api_error", "Não foi possível resolver o alvo agora.")
        return

    try:
        result = api.verify_user(target_id)  # Exactly one verification call for the confirmed target.
    except TelegramAPIError as exc:
        _record_api_outcome(store, owner_id, exc)
        error_class = _classify_verification_error(exc)
        _send_error(api, chat_id, error_class, "A verificação do alvo não foi concluída.")
        return

    if result is not True:
        _send_error(api, chat_id, "verification_rejected", "verifyUser não retornou True.")
        return
    store.record_verification_success(target_id, owner_id, "third_party")
    _send(api, chat_id, f"Sucesso: verifyUser retornou True para o alvo {target_id}.")


def handle_message(
    api: TelegramBotAPI,
    store: EventStore,
    settings: Settings,
    message: dict,
    bot_username: str,
) -> None:
    text = message.get("text")
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    sender_id = sender.get("id")
    chat_id = chat.get("id")
    if not isinstance(text, str) or not isinstance(sender_id, int) or not isinstance(chat_id, int):
        return

    command, argument = _command_parts(text)
    if command not in {"/start", "/verifyme", "/verify"}:
        return

    if sender_id not in settings.owner_ids:
        if command == "/start" and _is_private_1to1(message):
            _send(api, chat_id, "Conversa iniciada. Nenhuma verificação foi aplicada.")
        else:
            _send_error(api, chat_id, "unauthorized_sender", "Somente owners autorizam verificações.")
        return

    if not _is_private_1to1(message):
        _send_error(
            api,
            chat_id,
            "non_private_chat",
            "A operação deve ser iniciada em conversa privada 1:1 com o bot.",
            _open_private_keyboard(bot_username),
        )
        return

    if command == "/start":
        _render_owner_start(api, store, settings, sender_id, chat_id)
        return
    if command == "/verifyme":
        execute_owner_self_verification(api, store, sender_id, chat_id)
        return
    prepare_third_party_confirmation(api, store, settings, sender_id, chat_id, argument, bot_username)


def handle_callback_query(
    api: TelegramBotAPI,
    store: EventStore,
    settings: Settings,
    query: dict,
    bot_username: str,
) -> None:
    query_id = query.get("id")
    sender = query.get("from") or {}
    message = query.get("message") or {}
    data = query.get("data")
    sender_id = sender.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not isinstance(query_id, str) or not isinstance(data, str) or not isinstance(sender_id, int) or not isinstance(chat_id, int):
        return

    try:
        api.answer_callback_query(query_id)
    except TelegramAPIError:
        pass

    if sender_id not in settings.owner_ids:
        _send_error(api, chat_id, "unauthorized_sender", "Somente owners autorizam verificações.")
        return
    if chat.get("type") != "private" or chat_id != sender_id:
        _send_error(
            api,
            chat_id,
            "non_private_chat",
            "A operação deve ocorrer em conversa privada 1:1.",
            _open_private_keyboard(bot_username),
        )
        return

    if data == "owner:self":
        execute_owner_self_verification(api, store, sender_id, chat_id)
        return

    if data.startswith("cancel:"):
        _send(api, chat_id, "Verificação cancelada. Nenhum alvo foi alterado.")
        return

    if not data.startswith("verify:"):
        _send_error(api, chat_id, "verification_rejected", "Confirmação inválida.")
        return
    target_id = _parse_target_id(data.split(":", 1)[1])
    if target_id is None:
        _send_error(api, chat_id, "invalid_target_id", "O ID confirmado é inválido.")
        return
    execute_confirmed_third_party(api, store, settings, sender_id, chat_id, target_id, bot_username)


def prepare(api: TelegramBotAPI) -> str:
    identity = api.get_me()
    username = identity.get("username")
    if not isinstance(username, str) or not username:
        raise TelegramAPIError("getMe did not return the bot username required for Telegram links")
    api.delete_webhook()
    return username


def run(api: TelegramBotAPI, store: EventStore, settings: Settings, bot_username: str) -> None:
    offset: int | None = None
    LOGGER.info("Verifier worker started")
    while not _STOP:
        try:
            for update in api.get_updates(offset):
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    raise TelegramAPIError("Update without valid update_id")
                offset = update_id + 1
                message = update.get("message")
                if isinstance(message, dict):
                    handle_message(api, store, settings, message, bot_username)
                callback_query = update.get("callback_query")
                if isinstance(callback_query, dict):
                    handle_callback_query(api, store, settings, callback_query, bot_username)
        except TelegramAPIError as exc:
            LOGGER.warning("Telegram API error: %s", exc.description)
            time.sleep(2)
        except StateStoreError as exc:
            LOGGER.error("State store error: %s", exc)
            time.sleep(2)
        except Exception:
            LOGGER.exception("Unexpected worker error")
            time.sleep(2)
    LOGGER.info("Verifier worker stopped")


def _stop_handler(_signum: int, _frame: object) -> None:
    global _STOP
    _STOP = True


def main() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    api = TelegramBotAPI(settings.token)
    store = EventStore(settings.state_path)
    bot_username = prepare(api)
    run(api, store, settings, bot_username)


if __name__ == "__main__":
    main()
