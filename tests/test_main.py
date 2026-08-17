import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from main import (
    MAX_TELEGRAM_USER_ID,
    Settings,
    TelegramAPIError,
    TelegramBotAPI,
    VerificationSummary,
    _with_transient_retry,
    handle_message,
    load_settings,
    prepare,
    verify_targets,
)


BOT_USERNAME = "VerifierBot"


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
    def __init__(self, outcomes=None):
        self.outcomes = {key: list(value) for key, value in (outcomes or {}).items()}
        self.verified = []
        self.messages = []
        self.get_me_result = {"id": 777, "is_bot": True, "username": BOT_USERNAME}
        self.webhook_deleted = False

    def verify_user(self, user_id):
        self.verified.append(user_id)
        queue = self.outcomes.get(user_id, [])
        if queue:
            outcome = queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome is not True:
                raise TelegramAPIError("verifyUser did not return True")

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def get_me(self):
        return self.get_me_result

    def delete_webhook(self):
        self.webhook_deleted = True


class ProductTests(unittest.TestCase):
    def test_configuration_is_exact_and_ids_are_valid(self):
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "token",
            "VERIFICATION_OWNER_IDS": "1,2",
            "VERIFICATION_EXECUTIVE_IDS": "2,3,3",
        })
        self.assertEqual(settings.owner_ids, (1, 2))
        self.assertEqual(settings.targets, (1, 2, 3))
        with self.assertRaises(ValueError):
            load_settings({"TELEGRAM_BOT_TOKEN": "token", "VERIFICATION_OWNER_IDS": "1"})
        with self.assertRaises(ValueError):
            load_settings({
                "TELEGRAM_BOT_TOKEN": "token",
                "VERIFICATION_OWNER_IDS": f"1,{MAX_TELEGRAM_USER_ID + 1}",
            })

    def test_verify_user_calls_official_method_with_exact_payload(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": True})) as urlopen:
            api.verify_user(123456789)
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/verifyUser"))
        self.assertEqual(json.loads(request.data), {"user_id": 123456789})

    def test_verify_user_requires_literal_true(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": False})):
            with self.assertRaises(TelegramAPIError):
                api.verify_user(1)

    def test_send_message_passes_inline_keyboard_to_telegram(self):
        api = TelegramBotAPI("secret")
        markup = {"inline_keyboard": [[{"text": "Abrir", "url": "https://t.me/TestBot"}]]}
        with patch(
            "main.urllib.request.urlopen",
            return_value=Response({"ok": True, "result": {"message_id": 1}}),
        ) as urlopen:
            api.send_message(55, "texto", markup)
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload["reply_markup"], markup)

    def test_http_error_preserves_telegram_permission_error(self):
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

    def test_permission_missing_stops_batch_without_claiming_remaining_targets(self):
        api = ScriptedAPI({1: [TelegramAPIError("Forbidden: BOT_VERIFIER_FORBIDDEN", 403)]})
        summary = verify_targets(api, (1, 2, 3))
        self.assertEqual(summary, VerificationSummary(3, 0, 1, 2, 0, True))
        self.assertEqual(api.verified, [1])

    def test_permission_failure_after_success_preserves_completed_count_in_message(self):
        api = ScriptedAPI({
            2: [TelegramAPIError("Forbidden: BOT_VERIFIER_FORBIDDEN", 403)],
        })
        settings = Settings("token", (1, 2), (3,))
        handle_message(
            api,
            settings,
            {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1, "type": "private"}},
            BOT_USERNAME,
        )
        self.assertEqual(api.verified, [1, 2])
        message = api.messages[-1][1]
        self.assertIn("Verificados antes da interrupção: 1", message)
        self.assertIn("Falhas: 1", message)
        self.assertIn("Não tentados: 1", message)

    def test_inaccessible_target_is_counted_and_other_targets_continue(self):
        api = ScriptedAPI({2: [TelegramAPIError("Bad Request: PEER_ID_INVALID", 400)]})
        summary = verify_targets(api, (1, 2, 3))
        self.assertEqual(summary, VerificationSummary(3, 2, 1, 0, 1, False))
        self.assertEqual(api.verified, [1, 2, 3])

    @patch("main.time.sleep")
    def test_transient_error_retries_target_and_then_succeeds(self, sleep):
        api = ScriptedAPI({2: [TelegramAPIError("Too Many Requests", 429, {"retry_after": 4}), True]})
        summary = verify_targets(api, (1, 2, 3))
        self.assertTrue(summary.complete)
        self.assertEqual(api.verified, [1, 2, 2, 3])
        sleep.assert_called_once_with(4)

    @patch("main.time.sleep")
    def test_transient_error_exhaustion_stays_failure(self, sleep):
        api = ScriptedAPI({2: [
            TelegramAPIError("server", 500),
            TelegramAPIError("server", 500),
            TelegramAPIError("server", 500),
        ]})
        summary = verify_targets(api, (1, 2, 3))
        self.assertEqual(summary.succeeded, 2)
        self.assertEqual(summary.failed, 1)
        self.assertFalse(summary.complete)
        self.assertEqual(api.verified, [1, 2, 2, 2, 3])
        self.assertEqual(sleep.call_count, 2)

    def test_non_owner_cannot_trigger_in_private(self):
        api = ScriptedAPI()
        settings = Settings("token", (1, 2), (3,))
        handle_message(
            api,
            settings,
            {"text": "/verify", "from": {"id": 99}, "chat": {"id": 99, "type": "private"}},
            BOT_USERNAME,
        )
        self.assertEqual(api.verified, [])
        self.assertEqual(api.messages[-1][1], "Ação não autorizada.")

    def test_owner_verify_in_group_is_redirected_to_private_with_button(self):
        api = ScriptedAPI()
        settings = Settings("token", (1, 2), (3,))
        handle_message(
            api,
            settings,
            {"text": "/verify", "from": {"id": 1}, "chat": {"id": -100123, "type": "supergroup"}},
            BOT_USERNAME,
        )
        self.assertEqual(api.verified, [])
        self.assertEqual(len(api.messages), 1)
        _, text, markup = api.messages[-1]
        self.assertIn("conversa privada", text)
        button = markup["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Abrir conversa com o bot")
        self.assertEqual(button["url"], f"https://t.me/{BOT_USERNAME}?start=verify")

    def test_owner_only_gets_success_after_all_targets_succeed(self):
        api = ScriptedAPI()
        settings = Settings("token", (1, 2), (3,))
        handle_message(
            api,
            settings,
            {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1, "type": "private"}},
            BOT_USERNAME,
        )
        self.assertEqual(api.verified, [1, 2, 3])
        self.assertEqual(api.messages[-1][1], "Verificação concluída com sucesso para todos os 3 alvos.")

    def test_inaccessible_feedback_tells_target_to_start_and_provides_share_button(self):
        api = ScriptedAPI({2: [TelegramAPIError("Bad Request: PEER_ID_INVALID", 400)]})
        settings = Settings("token", (1, 2), (3,))
        handle_message(
            api,
            settings,
            {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1, "type": "private"}},
            BOT_USERNAME,
        )
        _, message, markup = api.messages[-1]
        self.assertIn("Verificação incompleta", message)
        self.assertIn("Sucesso: 2", message)
        self.assertIn("Falhas: 1", message)
        self.assertIn("Alvos ainda não acessíveis ao bot: 1", message)
        self.assertIn("enviar /start", message)
        button = markup["inline_keyboard"][0][0]
        self.assertEqual(button["text"], "Enviar instrução ao alvo")
        self.assertTrue(button["url"].startswith("https://t.me/share/url?"))
        parsed = urllib.parse.urlparse(button["url"])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["url"][0], f"https://t.me/{BOT_USERNAME}?start=verify")
        self.assertIn("verificação institucional", query["text"][0])

    def test_start_is_only_acknowledged_for_configured_targets_in_private(self):
        api = ScriptedAPI()
        settings = Settings("token", (1, 2), (3,))
        handle_message(
            api,
            settings,
            {"text": "/start verify", "from": {"id": 3}, "chat": {"id": 3, "type": "private"}},
            BOT_USERNAME,
        )
        self.assertEqual(len(api.messages), 1)
        handle_message(
            api,
            settings,
            {"text": "/start", "from": {"id": 99}, "chat": {"id": 99, "type": "private"}},
            BOT_USERNAME,
        )
        self.assertEqual(len(api.messages), 1)

    def test_prepare_requires_username_for_links(self):
        api = ScriptedAPI()
        self.assertEqual(prepare(api), BOT_USERNAME)
        self.assertTrue(api.webhook_deleted)

        api = ScriptedAPI()
        api.get_me_result = {"id": 777, "is_bot": True}
        with self.assertRaises(TelegramAPIError):
            prepare(api)

    def test_invalid_json_and_missing_result_fail_closed(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response(b"not-json")):
            with self.assertRaises(TelegramAPIError):
                api.get_me()
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True})):
            with self.assertRaises(TelegramAPIError):
                api.get_me()

    @patch("main.time.sleep")
    def test_retry_helper_does_not_retry_nontransient_error(self, sleep):
        with self.assertRaises(TelegramAPIError):
            _with_transient_retry(lambda: (_ for _ in ()).throw(TelegramAPIError("Bad Request", 400)))
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
