from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Protocol

import requests

from config import AppConfig, CloudflareTempEmailConfig, DuckMailConfig


EMAIL_POLL_INTERVAL_SECONDS = 3


@dataclass(frozen=True)
class TempEmailInbox:
    address: str
    token: str


class TempEmailProvider(Protocol):
    def create_inbox(self, name: str) -> TempEmailInbox:
        ...

    def snapshot_message_ids(self, inbox: TempEmailInbox) -> set[str]:
        ...

    def poll_verification_code(
        self,
        inbox: TempEmailInbox,
        timeout_seconds: int = 180,
        known_message_ids: set[str] | None = None,
    ) -> str | None:
        ...


class CloudflareTempEmailProvider:
    _BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(self, config: CloudflareTempEmailConfig):
        self.config = config

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": self._BROWSER_UA}
        if self.config.custom_auth:
            headers["x-custom-auth"] = self.config.custom_auth
        if extra:
            headers.update(extra)
        return headers

    def create_inbox(self, name: str) -> TempEmailInbox:
        response = requests.post(
            f"{self.config.api_url}/admin/new_address",
            headers=self._headers(
                {"x-admin-auth": self.config.admin_auth, "Content-Type": "application/json"}
            ),
            json={"name": name, "domain": self.config.domain, "enablePrefix": False},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        address = data.get("address", "")
        token = data.get("jwt", "")
        if not address or not token:
            raise RuntimeError(f"Email creation failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def _list_mails(self, inbox: TempEmailInbox) -> list[dict]:
        response = requests.get(
            f"{self.config.api_url}/api/mails?limit=5&offset=0",
            headers=self._mail_headers(inbox),
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        mails = data.get("results") or data.get("data") or []
        return mails if isinstance(mails, list) else []

    def _mail_headers(self, inbox: TempEmailInbox) -> dict[str, str]:
        return self._headers(
            {
                "Authorization": f"Bearer {inbox.token}",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )

    def snapshot_message_ids(self, inbox: TempEmailInbox) -> set[str]:
        return _message_ids(self._list_mails(inbox))

    def poll_verification_code(
        self,
        inbox: TempEmailInbox,
        timeout_seconds: int = 180,
        known_message_ids: set[str] | None = None,
    ) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        known_ids = {str(message_id) for message_id in (known_message_ids or set())}
        poll_number = 0

        while time.monotonic() < deadline:
            poll_number += 1
            try:
                mails = self._list_mails(inbox)
                new_mails = [
                    (mail_id, mail)
                    for mail in mails
                    if (mail_id := _message_id(mail)) is not None and mail_id not in known_ids
                ]
                print(
                    f"  email poll #{poll_number}: messages={len(mails)}, new={len(new_mails)}",
                    flush=True,
                )

                headers = self._mail_headers(inbox)
                for mail_id, mail in new_mails:
                    raw = mail.get("raw") if isinstance(mail.get("raw"), str) else ""
                    if not raw:
                        detail_response = requests.get(
                            f"{self.config.api_url}/api/mail/{mail_id}",
                            headers=headers,
                            timeout=15,
                        )
                        detail_response.raise_for_status()
                        raw = detail_response.json().get("raw", "")
                    code = _extract_verification_code(raw)
                    if code:
                        print(
                            f"  email poll #{poll_number}: verification code={code} (message={mail_id})",
                            flush=True,
                        )
                        return code

                if not _wait_for_next_poll(deadline):
                    break
            except Exception as exc:
                print(f"  email poll #{poll_number} failed: {exc}", flush=True)
                if not _wait_for_next_poll(deadline):
                    break

        print("  email poll timed out: no new verification code", flush=True)
        return None


class DuckMailProvider:
    def __init__(self, config: DuckMailConfig):
        self.config = config

    def create_inbox(self, name: str) -> TempEmailInbox:
        address = f"{name}@{self.config.domain}"
        password = f"dm_{secrets.token_hex(8)}"

        response = requests.post(
            f"{self.config.api_url}/accounts",
            headers=self._account_headers(),
            json={"address": address, "password": password},
            timeout=15,
        )
        response.raise_for_status()

        token_response = requests.post(
            f"{self.config.api_url}/token",
            headers={"Content-Type": "application/json"},
            json={"address": address, "password": password},
            timeout=15,
        )
        token_response.raise_for_status()
        data = token_response.json()
        token = data.get("token", "")
        if not token:
            raise RuntimeError(f"DuckMail token acquisition failed: {data}")
        return TempEmailInbox(address=address, token=token)

    def _list_messages(self, inbox: TempEmailInbox) -> list[dict]:
        response = requests.get(
            f"{self.config.api_url}/messages?page=1",
            headers=self._mail_headers(inbox),
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        messages = data.get("hydra:member") or []
        return messages if isinstance(messages, list) else []

    def _mail_headers(self, inbox: TempEmailInbox) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {inbox.token}",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def snapshot_message_ids(self, inbox: TempEmailInbox) -> set[str]:
        return _message_ids(self._list_messages(inbox))

    def poll_verification_code(
        self,
        inbox: TempEmailInbox,
        timeout_seconds: int = 180,
        known_message_ids: set[str] | None = None,
    ) -> str | None:
        deadline = time.monotonic() + timeout_seconds
        known_ids = {str(message_id) for message_id in (known_message_ids or set())}
        poll_number = 0

        while time.monotonic() < deadline:
            poll_number += 1
            try:
                messages = self._list_messages(inbox)
                new_messages = [
                    (message_id, message)
                    for message in messages
                    if (message_id := _message_id(message)) is not None
                    and message_id not in known_ids
                ]
                print(
                    f"  email poll #{poll_number}: messages={len(messages)}, new={len(new_messages)}",
                    flush=True,
                )

                headers = self._mail_headers(inbox)
                for message_id, _message in new_messages:
                    detail_response = requests.get(
                        f"{self.config.api_url}/messages/{message_id}",
                        headers=headers,
                        timeout=15,
                    )
                    detail_response.raise_for_status()
                    detail = detail_response.json()
                    code = _extract_verification_code(_duckmail_message_body(detail))
                    if code:
                        print(
                            f"  email poll #{poll_number}: verification code={code} (message={message_id})",
                            flush=True,
                        )
                        return code

                if not _wait_for_next_poll(deadline):
                    break
            except Exception as exc:
                print(f"  email poll #{poll_number} failed: {exc}", flush=True)
                if not _wait_for_next_poll(deadline):
                    break

        print("  email poll timed out: no new verification code", flush=True)
        return None

    def _account_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers


def _message_id(message: dict) -> str | None:
    for key in ("id", "_id"):
        value = message.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _message_ids(messages: list[dict]) -> set[str]:
    return {
        message_id
        for message in messages
        if (message_id := _message_id(message)) is not None
    }


def _wait_for_next_poll(deadline: float) -> bool:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(EMAIL_POLL_INTERVAL_SECONDS, remaining))
    return time.monotonic() < deadline


def _extract_verification_code(raw_message: str) -> str | None:
    clean = re.sub(r"=\r?\n", "", raw_message)
    index = clean.lower().find("verification code")
    if index >= 0:
        snippet = clean[index : index + 500]
        match = re.search(r"(\d{3})\s*[-–]\s*(\d{3})", snippet)
        if match:
            return match.group(1) + match.group(2)
    match = re.search(r"(?<!\d)(\d{3})[-–](\d{3})(?!\d)", clean)
    if match:
        return match.group(1) + match.group(2)
    return None


def _duckmail_message_body(detail: dict) -> str:
    parts: list[str] = []
    text = detail.get("text")
    if isinstance(text, str) and text.strip():
        parts.append(text)

    html = detail.get("html") or []
    if isinstance(html, list):
        for item in html:
            if isinstance(item, str) and item.strip():
                parts.append(item)
    elif isinstance(html, str) and html.strip():
        parts.append(html)

    return "\n".join(parts)


def build_email_provider(config: AppConfig) -> TempEmailProvider:
    if config.email_provider == "cloudflare_temp_email":
        return CloudflareTempEmailProvider(config.cloudflare_temp_email)
    if config.email_provider == "duckmail":
        return DuckMailProvider(config.duckmail)
    raise ValueError(f"Unsupported email provider: {config.email_provider}")
