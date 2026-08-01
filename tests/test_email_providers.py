import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from config import CloudflareTempEmailConfig, DuckMailConfig
from email_providers import CloudflareTempEmailProvider, DuckMailProvider, TempEmailInbox


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class EmailPollingTests(unittest.TestCase):
    inbox = TempEmailInbox(address="test@example.com", token="token")

    def test_cloudflare_ignores_old_code_and_returns_new_code(self) -> None:
        provider = CloudflareTempEmailProvider(
            CloudflareTempEmailConfig(
                api_url="https://mail.example.com",
                admin_auth="admin",
                domain="example.com",
            )
        )
        old_mail = {"id": "old", "raw": "Verification code: 111-222"}
        new_mail = {"id": "new", "raw": "Verification code: 333-444"}
        responses = [
            FakeResponse({"results": [old_mail]}),
            FakeResponse({"results": [old_mail]}),
            FakeResponse({"results": [new_mail, old_mail]}),
        ]
        clock = FakeClock()
        output = io.StringIO()

        with (
            patch("email_providers.requests.get", side_effect=responses) as get,
            patch("email_providers.time.monotonic", side_effect=clock.monotonic),
            patch("email_providers.time.sleep", side_effect=clock.sleep) as sleep,
            redirect_stdout(output),
        ):
            known_ids = provider.snapshot_message_ids(self.inbox)
            code = provider.poll_verification_code(
                self.inbox,
                timeout_seconds=30,
                known_message_ids=known_ids,
            )

        self.assertEqual(known_ids, {"old"})
        self.assertEqual(code, "333444")
        sleep.assert_called_once_with(3)
        self.assertEqual(get.call_count, 3)
        self.assertIn("email poll #1: messages=1, new=0", output.getvalue())
        self.assertIn("email poll #2: verification code=333444", output.getvalue())

    def test_duckmail_reads_only_new_message(self) -> None:
        provider = DuckMailProvider(
            DuckMailConfig(
                api_url="https://duckmail.example.com",
                domain="example.com",
                api_key=None,
            )
        )
        responses = [
            FakeResponse({"hydra:member": [{"id": "old"}]}),
            FakeResponse({"hydra:member": [{"id": "old"}]}),
            FakeResponse({"hydra:member": [{"id": "new"}, {"id": "old"}]}),
            FakeResponse({"text": "Your verification code is 987-654"}),
        ]
        clock = FakeClock()

        with (
            patch("email_providers.requests.get", side_effect=responses),
            patch("email_providers.time.monotonic", side_effect=clock.monotonic),
            patch("email_providers.time.sleep", side_effect=clock.sleep) as sleep,
        ):
            known_ids = provider.snapshot_message_ids(self.inbox)
            code = provider.poll_verification_code(
                self.inbox,
                timeout_seconds=30,
                known_message_ids=known_ids,
            )

        self.assertEqual(known_ids, {"old"})
        self.assertEqual(code, "987654")
        sleep.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
