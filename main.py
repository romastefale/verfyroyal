import json
import logging
import os
import signal
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable


LOGGER = logging.getLogger("verfyroyal")
_STOP = False


class TelegramAPIError(RuntimeError):
    def __init__(self, description: str, error_code: int | None = None, parameters: dict | None = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.parameters = parameters or {}


@dataclass(frozen=True)
class Settings:
    token: str
    owner_ids: tuple[int, ...]
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
            raise ValueError(f"{name} must contain comma-separated integer Telegram user IDs") from exc
        if value <= 0:
            raise ValueError(f"{name} must contain positive Telegram user IDs")
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

    executives = _parse_ids(source.get("VERIFICATION_EXECUTIVE_IDS", ""), "VERIFICATION_EXECUTIVE_IDS", False)
    level = source.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    return Settings(token=token, owner_ids=owners, executive_ids=executives, log_level=level)


class TelegramBotAPI:
    def __init__(self, token: str, timeout: int = 40):
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    @staticmethod
    def _decode_response(raw: bytes, http_status: int | None = None) -> dict:
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            status = f" HTTP {http_status}" if http_status is not None else ""
            raise TelegramAPIError(f"Invalid Telegram API response{status}", http_status) from exc
        if not isinstance(decoded, dict):
            raise TelegramAPIError("Invalid Telegram API response shape", http_status)
        return decoded

    def call(self, method: str, payload: dict | None = None) -> object:
        data = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = self._decode_response(response.read(), getattr(response, "status", None))
        except urllib.error.HTTPError as exc:
            result = self._decode_response(exc.read(), exc.code)
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise TelegramAPIError(f"Network error contacting Telegram: {reason}") from exc

        if result.get("ok") is not True:
            raise TelegramAPIError(
                str(result.get("description", "Telegram API request failed")),
                result.get("error_code") if isinstance(result.get("error_code"), int) else None,
                result.get("parameters") if isinstance(result.get("parameters"), dict) else None,
            )
        if "result" not in result:
            raise TelegramAPIError("Telegram API response missing result")
        return result["result"]

    def get_me(self) -> dict:
        result = self.call("getMe")
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise TelegramAPIError("Telegram getMe returned an invalid bot identity")
        return result

    def get_updates(self, offset: int | None) -> list[dict]:
        payload: dict[str, object] = {"timeout": 30, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramAPIError("Telegram getUpdates returned an invalid result")
        return result

    def send_message(self, chat_id: int, text: str) -> None:
        self.call("sendMessage", {"chat_id": chat_id, "text": text})

    def verify_user(self, user_id: int) -> bool:
        result = self.call("verifyUser", {"user_id": user_id})
        if result is not True:
            raise TelegramAPIError("Telegram verifyUser did not return True")
        return True


def _command(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].lower()


def verify_targets(api: TelegramBotAPI, targets: Iterable[int]) -> tuple[int, int, bool]:
    succeeded = 0
    failed = 0
    permission_missing = False

    for user_id in targets:
        try:
            api.verify_user(user_id)
            succeeded += 1
        except TelegramAPIError as exc:
            description = exc.description.upper()
            if "BOT_VERIFIER_FORBIDDEN" in description or ("VERIFIER" in description and "FORBIDDEN" in description):
                permission_missing = True
                failed += 1
                break
            if exc.error_code == 429:
                retry_after = exc.parameters.get("retry_after", 1)
                retry_after = retry_after if isinstance(retry_after, int) and retry_after > 0 else 1
                time.sleep(retry_after)
                try:
                    api.verify_user(user_id)
                    succeeded += 1
                except TelegramAPIError:
                    failed += 1
            else:
                failed += 1

    return succeeded, failed, permission_missing


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

    succeeded, failed, permission_missing = verify_targets(api, settings.targets)
    if permission_missing:
        api.send_message(chat_id, "A capacidade oficial de verificador ainda não está ativa no Telegram.")
        return

    if failed:
        api.send_message(chat_id, f"Verificação incompleta. Sucesso: {succeeded}. Falhas: {failed}.")
        return

    api.send_message(chat_id, f"Verificação concluída com sucesso para todos os {succeeded} alvos.")


def run(api: TelegramBotAPI, settings: Settings) -> None:
    offset: int | None = None
    LOGGER.info("Verifier worker started. Configured targets: %d", len(settings.targets))

    while not _STOP:
        try:
            updates = api.get_updates(offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
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
    api.get_me()
    run(api, settings)


if __name__ == "__main__":
    main()
