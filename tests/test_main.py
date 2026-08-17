import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from main import (
    EventStore,
    Settings,
    TelegramAPIError,
    TelegramBotAPI,
    build_inventory,
    capability_from_events,
    handle_callback_query,
    handle_message,
    load_settings,
    verified_ids_from_events,
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
        self.verify_calls = []
        self.messages = []
        self.callbacks = []
        self.chats = {}
        self.verify_outcomes = {}

    def get_chat(self, user_id):
        result = self.chats.get(user_id)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise TelegramAPIError("Bad Request: PEER_ID_INVALID", 400)
        return result

    def verify_user(self, user_id):
        self.verify_calls.append(user_id)
        outcome = self.verify_outcomes.get(user_id, True)
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is not True:
            raise TelegramAPIError("verifyUser did not return True", 400)
        return True

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text, reply_markup))

    def answer_callback_query(self, query_id, text=None):
        self.callbacks.append((query_id, text))


def private_message(sender_id, text):
    return {
        "text": text,
        "from": {"id": sender_id},
        "chat": {"id": sender_id, "type": "private"},
    }


def group_message(sender_id, text):
    return {
        "text": text,
        "from": {"id": sender_id},
        "chat": {"id": -100123, "type": "supergroup"},
    }


def callback(sender_id, data):
    return {
        "id": "cb1",
        "data": data,
        "from": {"id": sender_id},
        "message": {"chat": {"id": sender_id, "type": "private"}},
    }


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "events.jsonl")
        self.store = EventStore(self.path)
        self.store.ensure_ready()
        self.settings = Settings("token", (1, 2), (3, 4), self.path)
        self.api = ScriptedAPI()
        for uid, name in [
            (1, "Owner One"),
            (2, "Owner Two"),
            (3, "Exec Three"),
            (4, "Exec Four"),
            (9, "Target Nine"),
        ]:
            first, *rest = name.split()
            self.api.chats[uid] = {
                "id": uid,
                "type": "private",
                "first_name": first,
                "last_name": " ".join(rest),
                "username": f"user{uid}",
            }

    def tearDown(self):
        self.temp.cleanup()

    def mark_verified(self, user_id, actor_id=1, mode="test"):
        self.store.record_interaction(user_id, "test")
        self.store.record_verification_success(user_id, actor_id, mode)

    def mark_started(self, user_id):
        self.store.record_interaction(user_id, "start")

    def test_start_does_not_verify_third_party(self):
        handle_message(self.api, self.store, self.settings, private_message(1, "/start"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])

    def test_capability_missing_blocks_verify(self):
        self.mark_verified(1)
        self.store.record_capability_missing(1)
        self.mark_started(9)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("capability_missing", self.api.messages[-1][1])

    def test_owner_unverified_blocks_verify(self):
        self.mark_verified(2, actor_id=2)
        self.mark_started(9)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("owner_not_verified", self.api.messages[-1][1])

    def test_verify_is_unitary_and_requires_confirmation(self):
        self.mark_verified(1)
        self.mark_started(9)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        data = self.api.messages[-1][2]["inline_keyboard"][0][0]["callback_data"]
        self.assertEqual(data, "verify:1:9")
        handle_callback_query(self.api, self.store, self.settings, callback(1, data), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [9])

    def test_confirmation_is_bound_to_owner(self):
        self.mark_verified(1)
        self.mark_verified(2, actor_id=2)
        self.mark_started(9)
        handle_callback_query(self.api, self.store, self.settings, callback(2, "verify:1:9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("unauthorized_sender", self.api.messages[-1][1])

    def test_success_requires_literal_true(self):
        self.mark_verified(1)
        self.mark_started(9)
        self.api.verify_outcomes[9] = False
        handle_callback_query(self.api, self.store, self.settings, callback(1, "verify:1:9"), "VerifierBot")
        self.assertNotIn(9, verified_ids_from_events(self.store.read_events()))
        self.assertIn("verification_rejected", self.api.messages[-1][1])

    def test_target_without_start_is_inaccessible(self):
        self.mark_verified(1)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("target_inaccessible", self.api.messages[-1][1])

    def test_peer_id_invalid_is_inaccessible(self):
        self.mark_verified(1)
        self.mark_started(9)
        self.api.chats.pop(9)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("target_inaccessible", self.api.messages[-1][1])

    def test_transient_resolution_error_is_classified(self):
        self.mark_verified(1)
        self.mark_started(9)
        self.api.chats[9] = TelegramAPIError("server", 500)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("transient_api_error", self.api.messages[-1][1])

    def test_nontransient_resolution_error_is_not_called_transient(self):
        self.mark_verified(1)
        self.mark_started(9)
        self.api.chats[9] = TelegramAPIError("Bad Request", 400)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("target_resolution_error", self.api.messages[-1][1])

    def test_inventory_contains_only_persisted_successes(self):
        self.mark_verified(1)
        self.mark_started(3)
        inventory = build_inventory(self.api, self.store, self.settings)
        self.assertEqual([item.user_id for item in inventory.verified], [1])
        self.assertEqual(inventory.pending_count, 3)

    def test_non_owner_cannot_authorize(self):
        self.mark_started(9)
        handle_message(self.api, self.store, self.settings, private_message(99, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("unauthorized_sender", self.api.messages[-1][1])

    def test_non_owner_start_records_interaction_without_verification(self):
        handle_message(self.api, self.store, self.settings, private_message(9, "/start"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertEqual(self.api.messages[-1][1], "Conversa iniciada.")

    def test_owner_self_verifies_only_sender(self):
        handle_message(self.api, self.store, self.settings, private_message(1, "/verifyme"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [1])
        self.assertIn(1, verified_ids_from_events(self.store.read_events()))
        self.assertEqual(capability_from_events(self.store.read_events()), "active")

    def test_owner_self_failure_is_not_persisted(self):
        self.api.verify_outcomes[1] = TelegramAPIError("Forbidden: BOT_VERIFIER_FORBIDDEN", 403)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verifyme"), "VerifierBot")
        self.assertNotIn(1, verified_ids_from_events(self.store.read_events()))
        self.assertEqual(capability_from_events(self.store.read_events()), "missing")

    def test_owner_cannot_use_third_party_flow_for_owner(self):
        self.mark_verified(1)
        self.mark_started(2)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 2"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("invalid_target_id", self.api.messages[-1][1])

    def test_already_verified_target_is_not_verified_again(self):
        self.mark_verified(1)
        self.mark_verified(9)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("already_verified", self.api.messages[-1][1])

    def test_group_verify_is_rejected_and_redirected(self):
        handle_message(self.api, self.store, self.settings, group_message(1, "/verify 9"), "VerifierBot")
        self.assertEqual(self.api.verify_calls, [])
        self.assertIn("non_private_chat", self.api.messages[-1][1])
        self.assertIsNotNone(self.api.messages[-1][2])

    def test_invalid_target_id_is_explicit(self):
        self.mark_verified(1)
        handle_message(self.api, self.store, self.settings, private_message(1, "/verify abc"), "VerifierBot")
        self.assertIn("invalid_target_id", self.api.messages[-1][1])

    def test_transient_verify_error_is_not_persisted(self):
        self.mark_verified(1)
        self.mark_started(9)
        self.api.verify_outcomes[9] = TelegramAPIError("server", 500)
        handle_callback_query(self.api, self.store, self.settings, callback(1, "verify:1:9"), "VerifierBot")
        self.assertNotIn(9, verified_ids_from_events(self.store.read_events()))
        self.assertIn("transient_api_error", self.api.messages[-1][1])


class TransportTests(unittest.TestCase):
    def test_verify_user_exact_method_and_true(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": True})) as urlopen:
            self.assertTrue(api.verify_user(123))
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/verifyUser"))
        self.assertEqual(json.loads(request.data), {"user_id": 123})

    def test_verify_user_false_fails(self):
        api = TelegramBotAPI("secret")
        with patch("main.urllib.request.urlopen", return_value=Response({"ok": True, "result": False})):
            with self.assertRaises(TelegramAPIError):
                api.verify_user(123)

    def test_http_permission_error_is_preserved(self):
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

    def test_configuration(self):
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "t",
            "VERIFICATION_OWNER_IDS": "1,2",
            "VERIFICATION_EXECUTIVE_IDS": "3",
            "VERIFIER_STATE_PATH": "/state/events.jsonl",
        })
        self.assertEqual(settings.owner_ids, (1, 2))
        self.assertEqual(settings.executive_ids, (3,))
        self.assertEqual(settings.state_path, "/state/events.jsonl")

    def test_state_store_rejects_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.jsonl"
            path.write_text("{broken\n", encoding="utf-8")
            store = EventStore(str(path))
            with self.assertRaises(Exception):
                store.read_events()


if __name__ == "__main__":
    unittest.main()
