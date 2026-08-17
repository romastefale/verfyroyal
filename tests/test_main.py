import tempfile
import unittest
from pathlib import Path

import main as m


class FakeTelegram:
    def __init__(self):
        self.verify_calls = []
        self.messages = []
        self.chats = {}
        self.outcomes = {}

    def get_chat(self, uid):
        value = self.chats.get(uid)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise m.TelegramError("Bad Request: PEER_ID_INVALID", 400)
        return value

    def verify(self, uid):
        self.verify_calls.append(uid)
        value = self.outcomes.get(uid, True)
        if isinstance(value, Exception):
            raise value
        if value is not True:
            raise m.TelegramError("verifyUser did not return True", 400)
        return True

    def send(self, chat_id, text, markup=None):
        self.messages.append((chat_id, text, markup))


def pm(uid, text):
    return {"text": text, "from": {"id": uid}, "chat": {"id": uid, "type": "private"}}


def cb(uid, data):
    return {"id": "q1", "data": data, "from": {"id": uid}, "message": {"chat": {"id": uid, "type": "private"}}}


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "state.jsonl")
        self.store = m.Store(self.path)
        self.store.ready()
        self.settings = m.Settings("t", (1, 2), (3, 4), self.path)
        self.api = FakeTelegram()
        for uid, name in [(1, "Owner One"), (2, "Owner Two"), (3, "Exec Three"), (4, "Exec Four"), (9, "Target Nine")]:
            first, last = name.split()
            self.api.chats[uid] = {"id": uid, "first_name": first, "last_name": last, "username": f"u{uid}"}

    def tearDown(self):
        self.temp.cleanup()

    def start(self, uid):
        self.store.add("start", user_id=uid)

    def verified(self, uid, owner=1):
        self.store.add("start", user_id=uid)
        self.store.add("verified", user_id=uid, owner_id=owner)

    def test_start_never_verifies_third_party(self):
        m.handle_message(self.api, self.store, self.settings, pm(1, "/start"), "Bot")
        self.assertEqual(self.api.verify_calls, [])

    def test_start_non_owner_records_interaction(self):
        m.handle_message(self.api, self.store, self.settings, pm(9, "/start"), "Bot")
        self.assertIn(9, m.event_ids(self.store.read(), "start"))
        self.assertEqual(self.api.verify_calls, [])

    def test_owner_self_only_self(self):
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verifyme"), "Bot")
        self.assertEqual(self.api.verify_calls, [1])
        self.assertIn(1, m.event_ids(self.store.read(), "verified"))

    def test_owner_self_false_not_success(self):
        self.api.outcomes[1] = False
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verifyme"), "Bot")
        self.assertNotIn(1, m.event_ids(self.store.read(), "verified"))

    def test_capability_missing_recorded(self):
        self.api.outcomes[1] = m.TelegramError("Forbidden: BOT_VERIFIER_FORBIDDEN", 403)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verifyme"), "Bot")
        self.assertEqual(m.capability(self.store.read()), "missing")

    def test_unverified_owner_cannot_verify_third_party(self):
        self.start(9)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verify 9"), "Bot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("capability_unknown", self.api.messages[-1][1])

    def test_third_party_requires_explicit_valid_id(self):
        self.verified(1)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verify abc"), "Bot")
        self.assertIn("invalid_target_id", self.api.messages[-1][1])
        self.assertEqual(self.api.verify_calls, [])

    def test_target_requires_start(self):
        self.verified(1)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verify 9"), "Bot")
        self.assertIn("target_inaccessible", self.api.messages[-1][1])
        self.assertEqual(self.api.verify_calls, [])

    def test_verify_prepares_confirmation_without_call(self):
        self.verified(1)
        self.start(9)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verify 9"), "Bot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertEqual(self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"], "verify:1:9")

    def test_confirmation_affects_one_target(self):
        self.verified(1)
        self.start(9)
        m.handle_callback(self.api, self.store, self.settings, cb(1, "verify:1:9"), "Bot")
        self.assertEqual(self.api.verify_calls, [9])
        self.assertIn(9, m.event_ids(self.store.read(), "verified"))

    def test_confirmation_bound_to_owner(self):
        self.verified(1)
        self.verified(2, 2)
        self.start(9)
        m.handle_callback(self.api, self.store, self.settings, cb(2, "verify:1:9"), "Bot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("unauthorized_sender", self.api.messages[-1][1])

    def test_non_owner_cannot_authorize(self):
        m.handle_message(self.api, self.store, self.settings, pm(9, "/verify 3"), "Bot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("unauthorized_sender", self.api.messages[-1][1])

    def test_non_private_owner_blocked(self):
        msg = {"text": "/verify 9", "from": {"id": 1}, "chat": {"id": -100, "type": "supergroup"}}
        m.handle_message(self.api, self.store, self.settings, msg, "Bot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("non_private_chat", self.api.messages[-1][1])

    def test_pending_not_in_verified_inventory(self):
        self.verified(1)
        m.show_start(self.api, self.store, self.settings, 1)
        text = self.api.messages[-1][1]
        self.assertIn("Pendentes: 3", text)
        self.assertNotIn("Exec Three", text)

    def test_already_verified_not_reverified(self):
        self.verified(1)
        self.verified(9)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verify 9"), "Bot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("already_verified", self.api.messages[-1][1])

    def test_owner_not_allowed_as_third_party(self):
        self.verified(1)
        self.start(2)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verify 2"), "Bot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("invalid_target_id", self.api.messages[-1][1])

    def test_peer_invalid_classification(self):
        self.verified(1)
        self.start(9)
        self.api.chats.pop(9)
        m.handle_message(self.api, self.store, self.settings, pm(1, "/verify 9"), "Bot")
        self.assertIn("target_inaccessible", self.api.messages[-1][1])

    def test_verification_error_not_persisted(self):
        self.verified(1)
        self.start(9)
        self.api.outcomes[9] = m.TelegramError("server", 500)
        m.handle_callback(self.api, self.store, self.settings, cb(1, "verify:1:9"), "Bot")
        self.assertNotIn(9, m.event_ids(self.store.read(), "verified"))
        self.assertIn("transient_api_error", self.api.messages[-1][1])

    def test_store_is_success_source(self):
        self.start(9)
        self.assertNotIn(9, m.event_ids(self.store.read(), "verified"))
        self.store.add("verified", user_id=9, owner_id=1)
        self.assertIn(9, m.event_ids(self.store.read(), "verified"))


if __name__ == "__main__":
    unittest.main()
