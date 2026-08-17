import unittest
from unittest.mock import patch

from main import Settings, TelegramAPIError, _command, handle_message, load_settings, verify_targets


class FakeAPI:
    def __init__(self, verification_results=None):
        self.verification_results = verification_results or {}
        self.verified = []
        self.messages = []

    def verify_user(self, user_id):
        self.verified.append(user_id)
        result = self.verification_results.get(user_id, True)
        if isinstance(result, Exception):
            raise result
        return result

    def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


class SettingsTests(unittest.TestCase):
    def test_requires_exactly_two_distinct_owners(self):
        env = {
            "TELEGRAM_BOT_TOKEN": "token",
            "VERIFICATION_OWNER_IDS": "1,2",
            "VERIFICATION_EXECUTIVE_IDS": "3,4",
        }
        settings = load_settings(env)
        self.assertEqual(settings.owner_ids, (1, 2))
        self.assertEqual(settings.targets, (1, 2, 3, 4))

    def test_rejects_one_owner(self):
        with self.assertRaises(ValueError):
            load_settings({"TELEGRAM_BOT_TOKEN": "token", "VERIFICATION_OWNER_IDS": "1"})

    def test_deduplicates_targets(self):
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "token",
            "VERIFICATION_OWNER_IDS": "1,2",
            "VERIFICATION_EXECUTIVE_IDS": "2,3,3",
        })
        self.assertEqual(settings.targets, (1, 2, 3))


class CommandTests(unittest.TestCase):
    def test_command_normalization(self):
        self.assertEqual(_command("/verify@VerifierBot now"), "/verify")

    def test_non_owner_cannot_trigger(self):
        api = FakeAPI()
        settings = Settings("token", (1, 2), (3,))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 99}, "chat": {"id": 99}})
        self.assertEqual(api.verified, [])
        self.assertIn("não autorizada", api.messages[0][1])

    def test_owner_triggers_all_unique_targets(self):
        api = FakeAPI()
        settings = Settings("token", (1, 2), (2, 3))
        handle_message(api, settings, {"text": "/verify", "from": {"id": 1}, "chat": {"id": 1}})
        self.assertEqual(api.verified, [1, 2, 3])
        self.assertIn("Sucesso: 3", api.messages[-1][1])


class VerificationTests(unittest.TestCase):
    def test_permission_not_yet_enabled_is_reported_cleanly(self):
        api = FakeAPI({1: TelegramAPIError("Bad Request: BOT_VERIFIER_FORBIDDEN", 400)})
        result = verify_targets(api, [1, 2, 3])
        self.assertEqual(result, (0, 1, True))
        self.assertEqual(api.verified, [1])

    @patch("main.time.sleep")
    def test_rate_limit_retries_once(self, sleep):
        class RateLimitAPI(FakeAPI):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def verify_user(self, user_id):
                self.calls += 1
                if self.calls == 1:
                    raise TelegramAPIError("Too Many Requests", 429, {"retry_after": 1})
                return True

        api = RateLimitAPI()
        self.assertEqual(verify_targets(api, [1]), (1, 0, False))
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
