import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from main import (
    Settings,
    TelegramAPIError,
    TelegramBotAPI,
    _command,
    handle_message,
    load_settings,
    prepare,
    verify_targets,
)


class ScriptedAPI:
    def __init__(self, verification_results=None):
        self.verification_results = verification_results or {}
        self.verified = []
        self.messages = []
        self.get_me_result = {"id": 777, "is_bot": True, "username": "verifier"}
        self.webhook_deleted = False

    def verify_user(self, user_id):
        self.verified.append(user_id)
        result = self.verification_results.get(user_id, True)
        if isinstance(result, Exception):
            raise result
        return result

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))

    def get_me(self):
        if isinstance(self.get_me_result, Exception):
            raise self.get_me_result
        return self.get_me_result

    def delete_webhook(self):
        self.webhook_deleted = True


class Response:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FinalDeliveryTests(unittest.TestCase):
    """Treat a wrong final state as total failure, not as a partial pass."""

    def test_owner_command_requires_every_target_to_succeed_before_claiming_total_success(self):
        api = ScriptedAPI({1: True, 2: True, 3: True})
        settings = Settings("token", (1, 2), (3,))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1}})
        self.assertEqual(api.verified, [1, 2, 3])
        self.assertEqual(api.messages[-1][1], "Verificação concluída com sucesso para todos os 3 alvos.")

    def test_partial_failure_can_never_claim_total_success(self):
        api = ScriptedAPI({1: True, 2: False, 3: True})
        settings = Settings("token", (1, 2), (3,))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1}})
        self.assertEqual(api.verified, [1, 2, 3])
        self.assertEqual(api.messages[-1][1], "Verificação incompleta. Sucesso: 2. Falhas: 1. Total: 3.")

    def test_startup_requires_real_bot_identity_and_polling_compatibility(self):
        api = ScriptedAPI()
        identity = prepare(api)
        self.assertEqual(identity["id"], 777)
        self.assertTrue(api.webhook_deleted)

    def test_non_owner_never_reaches_verification(self):
        api = ScriptedAPI()
        settings = Settings("token", (1, 2), (3,))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 99}, "chat": {"id": 99}})
        self.assertEqual(api.verified, [])
        self.assertEqual(api.messages[-1][1], "Ação não autorizada.")


class RiskFocusedTests(unittest.TestCase):
    """Prefer failure-prone protocol boundaries over comfortable syntax checks."""

    def test_verify_user_calls_exact_official_method_and_payload_and_requires_literal_true(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": True})) as urlopen:
            self.assertTrue(api.verify_user(123456789))
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/verifyUser"))
        self.assertEqual(json.loads(request.data.decode()), {"user_id": 123456789})

        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": {"unexpected": True}})):
            self.assertFalse(api.verify_user(123456789))

    def test_malformed_json_is_failure(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response(b"not-json")):
            with self.assertRaisesRegex(TelegramAPIError, "invalid JSON"):
                api.get_me()

    def test_success_without_result_is_failure(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True})):
            with self.assertRaisesRegex(TelegramAPIError, "did not contain result"):
                api.get_me()

    def test_get_updates_wrong_shape_is_failure_not_empty_poll(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": {}})):
            with self.assertRaisesRegex(TelegramAPIError, "getUpdates returned an invalid result"):
                api.get_updates(None)

    def test_network_failure_is_not_success(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaisesRegex(TelegramAPIError, "Network error"):
                api.verify_user(1)

    def test_realistic_permission_error_stops_batch(self):
        api = ScriptedAPI({1: TelegramAPIError("Forbidden: BOT_VERIFIER_FORBIDDEN", 403)})
        self.assertEqual(verify_targets(api, [1, 2, 3]), (0, 1, True))
        self.assertEqual(api.verified, [1])

    @patch("main.time.sleep")
    def test_rate_limit_retries_only_the_affected_target_once(self, sleep):
        class RateLimited(ScriptedAPI):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def verify_user(self, user_id):
                self.verified.append(user_id)
                self.calls += 1
                if self.calls == 1:
                    raise TelegramAPIError("Too Many Requests", 429, {"retry_after": 2})
                return True

        api = RateLimited()
        self.assertEqual(verify_targets(api, [10]), (1, 0, False))
        self.assertEqual(api.verified, [10, 10])
        sleep.assert_called_once_with(2)

    def test_http_error_json_preserves_telegram_error_code_and_description(self):
        api = TelegramBotAPI("secret")
        error_body = io.BytesIO(json.dumps({
            "ok": False,
            "error_code": 403,
            "description": "Forbidden: BOT_VERIFIER_FORBIDDEN",
        }).encode())
        http_error = urllib.error.HTTPError("url", 403, "Forbidden", {}, error_body)
        with patch("main.urllib.request.urlopen", side_effect=http_error):
            with self.assertRaises(TelegramAPIError) as caught:
                api.verify_user(1)
        self.assertEqual(caught.exception.error_code, 403)
        self.assertIn("BOT_VERIFIER_FORBIDDEN", caught.exception.description)


class TransparencyTests(unittest.TestCase):
    """Failures must remain visible and remediable instead of being softened into a success-looking state."""

    def test_missing_token_fails_before_runtime(self):
        with self.assertRaisesRegex(ValueError, "TELEGRAM_BOT_TOKEN is required"):
            load_settings({"VERIFICATION_OWNER_IDS": "1,2"})

    def test_exactly_two_distinct_owners_are_required(self):
        for raw in ("1", "1,1", ""):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    load_settings({"TELEGRAM_BOT_TOKEN": "token", "VERIFICATION_OWNER_IDS": raw})

    def test_invalid_target_id_is_rejected_not_ignored(self):
        with self.assertRaisesRegex(ValueError, "positive Telegram user IDs"):
            load_settings({
                "TELEGRAM_BOT_TOKEN": "token",
                "VERIFICATION_OWNER_IDS": "1,2",
                "VERIFICATION_EXECUTIVE_IDS": "-3",
            })

    def test_targets_are_unique_and_complete(self):
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "token",
            "VERIFICATION_OWNER_IDS": "1,2",
            "VERIFICATION_EXECUTIVE_IDS": "2,3,3,4",
        })
        self.assertEqual(settings.targets, (1, 2, 3, 4))

    def test_command_normalization_does_not_expand_authority(self):
        self.assertEqual(_command("/verify@VerifierBot now"), "/verify")
        self.assertNotEqual(_command("verify"), "/verify")

    def test_get_me_rejects_non_bot_identity(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": {"id": 7, "is_bot": False}})):
            with self.assertRaisesRegex(TelegramAPIError, "valid bot identity"):
                api.get_me()

    def test_delete_webhook_must_return_true(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": False})):
            with self.assertRaisesRegex(TelegramAPIError, "deleteWebhook did not return True"):
                api.delete_webhook()


if __name__ == "__main__":
    unittest.main()
