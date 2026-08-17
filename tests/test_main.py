import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from main import Settings, TelegramAPIError, TelegramBotAPI, handle_message, load_settings, verify_targets


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def http_error(code, body):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return urllib.error.HTTPError("https://api.telegram.org/test", code, "error", {}, io.BytesIO(raw))


class RecordingAPI:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.verified = []
        self.messages = []

    def verify_user(self, user_id):
        self.verified.append(user_id)
        if not self.outcomes:
            raise AssertionError("Unexpected verify_user call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not True:
            raise TelegramAPIError("verifyUser did not return True")
        return True

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class SettingsTests(unittest.TestCase):
    def test_requires_token(self):
        with self.assertRaises(ValueError):
            load_settings({"VERIFICATION_OWNER_IDS": "1,2"})

    def test_requires_exactly_two_distinct_owners(self):
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "t",
            "VERIFICATION_OWNER_IDS": "1,2",
            "VERIFICATION_EXECUTIVE_IDS": "2,3,3",
        })
        self.assertEqual(settings.owner_ids, (1, 2))
        self.assertEqual(settings.targets, (1, 2, 3))
        with self.assertRaises(ValueError):
            load_settings({"TELEGRAM_BOT_TOKEN": "t", "VERIFICATION_OWNER_IDS": "1,1"})

    def test_rejects_invalid_ids(self):
        for bad in ["x,2", "0,2", "-1,2"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                load_settings({"TELEGRAM_BOT_TOKEN": "t", "VERIFICATION_OWNER_IDS": bad})


class TransportTests(unittest.TestCase):
    @patch("main.urllib.request.urlopen")
    def test_verify_user_sends_real_method_and_payload_shape(self, urlopen):
        urlopen.return_value = Response({"ok": True, "result": True})
        api = TelegramBotAPI("secret")
        self.assertTrue(api.verify_user(123456789))
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/verifyUser"))
        self.assertEqual(json.loads(request.data), {"user_id": 123456789})

    @patch("main.urllib.request.urlopen")
    def test_http_400_permission_error_is_not_success(self, urlopen):
        urlopen.side_effect = http_error(400, {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: BOT_VERIFIER_FORBIDDEN",
        })
        with self.assertRaises(TelegramAPIError) as ctx:
            TelegramBotAPI("secret").verify_user(1)
        self.assertIn("BOT_VERIFIER_FORBIDDEN", ctx.exception.description)
        self.assertEqual(ctx.exception.error_code, 400)

    @patch("main.urllib.request.urlopen")
    def test_http_429_parameters_are_preserved(self, urlopen):
        urlopen.side_effect = http_error(429, {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 7},
        })
        with self.assertRaises(TelegramAPIError) as ctx:
            TelegramBotAPI("secret").verify_user(1)
        self.assertEqual(ctx.exception.error_code, 429)
        self.assertEqual(ctx.exception.parameters["retry_after"], 7)

    @patch("main.urllib.request.urlopen")
    def test_malformed_json_fails_closed(self, urlopen):
        urlopen.return_value = Response(b"not-json")
        with self.assertRaises(TelegramAPIError):
            TelegramBotAPI("secret").verify_user(1)

    @patch("main.urllib.request.urlopen")
    def test_missing_result_fails_closed(self, urlopen):
        urlopen.return_value = Response({"ok": True})
        with self.assertRaises(TelegramAPIError):
            TelegramBotAPI("secret").verify_user(1)

    @patch("main.urllib.request.urlopen")
    def test_verify_user_false_is_failure_not_success(self, urlopen):
        urlopen.return_value = Response({"ok": True, "result": False})
        with self.assertRaises(TelegramAPIError):
            TelegramBotAPI("secret").verify_user(1)

    @patch("main.urllib.request.urlopen")
    def test_network_failure_is_failure(self, urlopen):
        urlopen.side_effect = urllib.error.URLError("offline")
        with self.assertRaises(TelegramAPIError) as ctx:
            TelegramBotAPI("secret").verify_user(1)
        self.assertIn("Network error", ctx.exception.description)

    @patch("main.urllib.request.urlopen")
    def test_get_me_rejects_invalid_identity(self, urlopen):
        urlopen.return_value = Response({"ok": True, "result": {"username": "bot"}})
        with self.assertRaises(TelegramAPIError):
            TelegramBotAPI("secret").get_me()


class FlowFailureTests(unittest.TestCase):
    def test_non_owner_never_reaches_verify(self):
        api = RecordingAPI([])
        handle_message(
            api,
            Settings("t", (1, 2), (3,)),
            {"text": "/verify", "from": {"id": 99}, "chat": {"id": 99}},
        )
        self.assertEqual(api.verified, [])
        self.assertIn("não autorizada", api.messages[-1][1])

    def test_all_success_requires_every_target_to_succeed(self):
        api = RecordingAPI([True, True, True])
        handle_message(
            api,
            Settings("t", (1, 2), (2, 3)),
            {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1}},
        )
        self.assertEqual(api.verified, [1, 2, 3])
        self.assertIn("todos os 3 alvos", api.messages[-1][1])

    def test_partial_failure_cannot_report_total_success(self):
        api = RecordingAPI([True, TelegramAPIError("server failure", 500), True])
        handle_message(
            api,
            Settings("t", (1, 2), (3,)),
            {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1}},
        )
        self.assertEqual(api.verified, [1, 2, 3])
        self.assertIn("incompleta", api.messages[-1][1])
        self.assertIn("Falhas: 1", api.messages[-1][1])
        self.assertNotIn("todos", api.messages[-1][1])

    def test_permission_missing_stops_immediately_and_never_reports_success(self):
        api = RecordingAPI([TelegramAPIError("Bad Request: BOT_VERIFIER_FORBIDDEN", 400), True])
        result = verify_targets(api, [1, 2])
        self.assertEqual(result, (0, 1, True))
        self.assertEqual(api.verified, [1])

    @patch("main.time.sleep")
    def test_rate_limit_retries_and_second_failure_remains_failure(self, sleep):
        api = RecordingAPI([
            TelegramAPIError("Too Many Requests", 429, {"retry_after": 3}),
            TelegramAPIError("server failure", 500),
        ])
        self.assertEqual(verify_targets(api, [1]), (0, 1, False))
        sleep.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
