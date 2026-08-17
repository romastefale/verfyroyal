import json
import logging
import os
import signal
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


LOGGER = logging.getLogger("verfyroyal")
_STOP = False
MAX_TELEGRAM_USER_ID = 0xFFFFFFFFFF


class TelegramAPIError(RuntimeError):
    def __init__(self, description: str, error_code: int | None = None, parameters: dict | None = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.parameters = parameters or {}


@dataclass(frozen=True)
class Settings:
    token: str
    owner_ids: tuple[int, int]
    executive_ids: tuple[int, ...]
    log_level: str = "INFO"

    @property
    def targets(self) -> tuple[int, ...]:
        return tuple(dict.fromkeys((*self.owner_ids, *self.executive_ids)))


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
    level = source.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    return Settings(token, (owners[0], owners[1]), executives, level)


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

        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramAPIError(
                str(body.get("description", "Telegram API request failed")) if isinstance(body, dict) else "Telegram returned an invalid response",
                body.get("error_code") if isinstance(body, dict) and isinstance(body.get("error_code"), int) else None,
                body.get("parameters") if isinstance(body, dict) and isinstance(body.get("parameters"), dict) else {},
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
        payload: dict[str, object] = {"timeout": 30, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        if not isinstance(result, list) or not all(isinstance(update, dict) for update in result):
            raise TelegramAPIError("getUpdates returned an invalid result")
        return result

    def get_chat(self, user_id: int) -> dict:
        result = self.call("getChat", {"chat_id": user_id})
        if not isinstance(result, dict) or result.get("id") != user_id or result.get("type") != "private":
            raise TelegramAPIError("getChat did not return the expected private user chat")
        return result

    def send_message(self, chat_id: int, text: str) -> None:
        result = self.call("sendMessage", {"chat_id": chat_id, "text": text})
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramAPIError("sendMessage did not return a valid Message")

    def verify_user(self, user_id: int) -> None:
        if self.call("verifyUser", {"user_id": user_id}) is not True:
            raise TelegramAPIError("verifyUser did not return True")


def _command(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].lower()


def _retry_after(exc: TelegramAPIError) -> int | None:
    if exc.error_code != 429:
        return None
    value = exc.parameters.get("retry_after")
    return value if isinstance(value, int) and value > 0 else 1


def _call_with_transient_retry(operation, attempts: int = 3) -> None:
    for attempt in range(attempts):
        try:
            operation()
            return
        except TelegramAPIError as exc:
            wait = _retry_after(exc)
            transient = wait is not None or exc.error_code is None or (exc.error_code >= 500 if exc.error_code is not None else False)
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(wait if wait is not None else 2 ** attempt)


def preflight_targets(api: TelegramBotAPI, targets: tuple[int, ...]) -> None:
    for user_id in targets:
        api.get_chat(user_id)


def verify_all(api: TelegramBotAPI, targets: tuple[int, ...]) -> None:
    preflight_targets(api, targets)
    for user_id in targets:
        _call_with_transient_retry(lambda uid=user_id: api.verify_user(uid))


def handle_message(api: TelegramBotAPI, settings: Settings, message: dict) -> None:
    text = message.get("text")
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    sender_id = sender.get("id")
    chat_id = chat.get("id")

    if not isinstance(text, str) or not isinstance(sender_id, int) or not isinstance(chat_id, int):
        return
    if _command(text) != "/verify":
        return
    if sender_id not in settings.owner_ids:
        api.send_message(chat_id, "Ação não autorizada.")
        return

    try:
        verify_all(api, settings.targets)
    except TelegramAPIError as exc:
        if exc.error_code == 403 and "BOT_VERIFIER_FORBIDDEN" in exc.description.upper():
            api.send_message(chat_id, "A capacidade oficial de verificador ainda não está ativa para este bot.")
            return
        if exc.error_code == 400 and "PEER_ID_INVALID" in exc.description.upper():
            api.send_message(chat_id, "Um ou mais alvos ainda não estão acessíveis ao bot no Telegram.")
            return
        api.send_message(chat_id, "A verificação não foi concluída. Nenhum resultado foi tratado como sucesso.")
        return

    api.send_message(chat_id, f"Verificação concluída com sucesso para todos os {len(settings.targets)} alvos.")


def prepare(api: TelegramBotAPI) -> None:
    api.get_me()
    api.delete_webhook()


def run(api: TelegramBotAPI, settings: Settings) -> None:
    offset: int | None = None
    LOGGER.info("Verifier worker started. Configured targets: %d", len(settings.targets))

    while not _STOP:
        try:
            for update in api.get_updates(offset):
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    raise TelegramAPIError("Update without valid update_id")
                offset = update_id + 1
                message = update.get("message")
                if isinstance(message, dict):
                    handle_message(api, settings, message)
        except TelegramAPIError as exc:
            LOGGER.warning("Telegram API error: %s", exc.description)
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
    prepare(api)
    run(api, settings)


if __name__ == "__main__":
    main()
