import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from main import (
    MAX_TELEGRAM_USER_ID,
    Settings,
    TelegramAPIError,
    TelegramBotAPI,
    _call_with_transient_retry,
    handle_message,
    load_settings,
    verify_all,
)


class Response:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ScriptedAPI:
    def __init__(self):
        self.known = set()
        self.verified = []
        self.messages = []
        self.fail_verify = {}

    def get_chat(self, user_id):
        if user_id not in self.known:
            raise TelegramAPIError("Bad Request: PEER_ID_INVALID", 400)
        return {"id": user_id, "type": "private"}

    def verify_user(self, user_id):
        error = self.fail_verify.get(user_id)
        if error:
            raise error
        self.verified.append(user_id)

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class ProductTests(unittest.TestCase):
    def test_configuration_requires_token_two_distinct_owners_and_valid_user_ids(self):
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "token",
            "VERIFICATION_OWNER_IDS": "1,2",
            "VERIFICATION_EXECUTIVE_IDS": "2,3,3",
        })
        self.assertEqual(settings.targets, (1, 2, 3))

        with self.assertRaises(ValueError):
            load_settings({"TELEGRAM_BOT_TOKEN": "token", "VERIFICATION_OWNER_IDS": "1"})
        with self.assertRaises(ValueError):
            load_settings({"TELEGRAM_BOT_TOKEN": "token", "VERIFICATION_OWNER_IDS": f"1,{MAX_TELEGRAM_USER_ID + 1}"})

    def test_verify_user_uses_official_method_and_requires_true(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": True})) as urlopen:
            api.verify_user(123)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/verifyUser"))
        self.assertEqual(json.loads(request.data), {"user_id": 123})

        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": False})):
            with self.assertRaises(TelegramAPIError):
                api.verify_user(123)

    def test_all_targets_are_preflighted_before_first_verification(self):
        api = ScriptedAPI()
        api.known = {1, 2}
        with self.assertRaises(TelegramAPIError):
            verify_all(api, (1, 2, 3))
        self.assertEqual(api.verified, [])

    def test_owner_gets_total_success_only_after_every_target_succeeds(self):
        api = ScriptedAPI()
        api.known = {1, 2, 3}
        settings = Settings("token", (1, 2), (3,))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1}})
        self.assertEqual(api.verified, [1, 2, 3])
        self.assertEqual(api.messages[-1][1], "Verificação concluída com sucesso para todos os 3 alvos.")

    def test_unknown_target_aborts_before_any_verification(self):
        api = ScriptedAPI()
        api.known = {1, 2}
        settings = Settings("token", (1, 2), (3,))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1}})
        self.assertEqual(api.verified, [])
        self.assertIn("não estão acessíveis", api.messages[-1][1])

    def test_non_owner_cannot_start_verification(self):
        api = ScriptedAPI()
        api.known = {1, 2, 3}
        settings = Settings("token", (1, 2), (3,))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 99}, "chat": {"id": 99}})
        self.assertEqual(api.verified, [])
        self.assertEqual(api.messages[-1][1], "Ação não autorizada.")

    @patch("main.time.sleep")
    def test_transient_429_is_retried_but_permission_error_is_not(self, sleep):
        calls = []

        def transient():
            calls.append(1)
            if len(calls) == 1:
                raise TelegramAPIError("Too Many Requests", 429, {"retry_after": 4})

        _call_with_transient_retry(transient)
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once_with(4)

        with self.assertRaises(TelegramAPIError):
            _call_with_transient_retry(lambda: (_ for _ in ()).throw(TelegramAPIError("BOT_VERIFIER_FORBIDDEN", 403)))

    def test_http_error_preserves_telegram_error(self):
        body = io.BytesIO(json.dumps({
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: BOT_VERIFIER_FORBIDDEN",
        }).encode("utf-8"))
        error = urllib.error.HTTPError("url", 403, "Forbidden", {}, body)
        with patch("main.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(TelegramAPIError) as caught:
                TelegramBotAPI("secret").verify_user(1)
        self.assertEqual(caught.exception.error_code, 403)
        self.assertIn("BOT_VERIFIER_FORBIDDEN", caught.exception.description)


if __name__ == "__main__":
    unittest.main()
