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

    if required and not values:
        raise ValueError(f"{name} is required")
    return tuple(dict.fromkeys(values))


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
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                result = json.loads(raw)
            except json.JSONDecodeError as parse_exc:
                raise TelegramAPIError(f"Telegram HTTP error {exc.code} with invalid JSON", exc.code) from parse_exc
            if not isinstance(result, dict):
                raise TelegramAPIError(f"Telegram HTTP error {exc.code} with invalid response shape", exc.code)
            raise TelegramAPIError(
                str(result.get("description", f"Telegram HTTP error {exc.code}")),
                int(result.get("error_code", exc.code)),
                result.get("parameters") if isinstance(result.get("parameters"), dict) else {},
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise TelegramAPIError(f"Network error contacting Telegram: {reason}") from exc

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramAPIError("Telegram returned invalid JSON") from exc

        # A malformed success response is not success. Refuse to infer or coerce it.
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise TelegramAPIError("Telegram returned an invalid response shape")
        if result["ok"] is not True:
            raise TelegramAPIError(
                str(result.get("description", "Telegram API request failed")),
                result.get("error_code") if isinstance(result.get("error_code"), int) else None,
                result.get("parameters") if isinstance(result.get("parameters"), dict) else {},
            )
        if "result" not in result:
            raise TelegramAPIError("Telegram success response did not contain result")
        return result["result"]

    def get_me(self) -> dict:
        result = self.call("getMe")
        if not isinstance(result, dict) or result.get("is_bot") is not True or not isinstance(result.get("id"), int):
            raise TelegramAPIError("getMe did not return a valid bot identity")
        return result

    def delete_webhook(self) -> None:
        # getUpdates and webhooks are mutually exclusive. Removing an old webhook makes the chosen polling mode explicit.
        result = self.call("deleteWebhook", {"drop_pending_updates": False})
        if result is not True:
            raise TelegramAPIError("deleteWebhook did not return True")

    def get_updates(self, offset: int | None) -> list[dict]:
        payload: dict[str, object] = {"timeout": 30, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self.call("getUpdates", payload)
        if not isinstance(result, list) or not all(isinstance(update, dict) for update in result):
            raise TelegramAPIError("getUpdates returned an invalid result")
        return result

    def send_message(self, chat_id: int, text: str) -> None:
        result = self.call("sendMessage", {"chat_id": chat_id, "text": text})
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramAPIError("sendMessage did not return a valid Message")

    def verify_user(self, user_id: int) -> bool:
        result = self.call("verifyUser", {"user_id": user_id})
        # Telegram documents verifyUser as returning True on success. False or any other shape is a failed verification.
        return result is True


def _command(text: str) -> str:
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    return first.split("@", 1)[0].lower()


def _is_permission_missing(exc: TelegramAPIError) -> bool:
    return exc.error_code == 403 and "BOT_VERIFIER_FORBIDDEN" in exc.description.upper()


def verify_targets(api: TelegramBotAPI, targets: Iterable[int]) -> tuple[int, int, bool]:
    succeeded = 0
    failed = 0
    permission_missing = False

    for user_id in targets:
        try:
            if api.verify_user(user_id):
                succeeded += 1
            else:
                failed += 1
        except TelegramAPIError as exc:
            if _is_permission_missing(exc):
                permission_missing = True
                failed += 1
                break
            if exc.error_code == 429:
                retry_after_raw = exc.parameters.get("retry_after", 1)
                retry_after = retry_after_raw if isinstance(retry_after_raw, int) and retry_after_raw > 0 else 1
                time.sleep(retry_after)
                try:
                    if api.verify_user(user_id):
                        succeeded += 1
                    else:
                        failed += 1
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
        api.send_message(chat_id, "A capacidade oficial de verificador ainda não está ativa para este bot.")
        return

    total = len(settings.targets)
    if succeeded == total and failed == 0:
        api.send_message(chat_id, f"Verificação concluída com sucesso para todos os {total} alvos.")
    else:
        # Never convert a partial result into a success statement. The counts are the evidence needed to retry/remediate.
        api.send_message(chat_id, f"Verificação incompleta. Sucesso: {succeeded}. Falhas: {failed}. Total: {total}.")


def prepare(api: TelegramBotAPI) -> dict:
    # Startup proves the token identifies a bot and clears the only Telegram mode that conflicts with getUpdates.
    identity = api.get_me()
    api.delete_webhook()
    return identity


def run(api: TelegramBotAPI, settings: Settings) -> None:
    offset: int | None = None
    LOGGER.info("Verifier worker started. Configured targets: %d", len(settings.targets))

    while not _STOP:
        try:
            updates = api.get_updates(offset)
            for update in updates:
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
    identity = prepare(api)
    LOGGER.info("Telegram bot identity confirmed. Bot ID: %s", identity["id"])
    run(api, settings)


if __name__ == "__main__":
    main()
