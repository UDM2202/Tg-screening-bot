"""Minimal Telegram Bot API client. Prints to the console when no token is configured."""

from __future__ import annotations

import html
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

Button = tuple[str, str]  # (label, url)


class Telegram:
    def __init__(self, client: httpx.AsyncClient, token: str, chat_id: str):
        self.client = client
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)
        self.base = f"https://api.telegram.org/bot{token}"

    async def _call(self, method: str, payload: dict[str, Any], http_timeout: float = 20) -> Any:
        resp = await self.client.post(f"{self.base}/{method}", json=payload, timeout=http_timeout)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method}: {body.get('description')}")
        return body["result"]

    async def send(
        self, text: str, buttons: list[list[Button]] | None = None, reply_to: int | None = None
    ) -> int | None:
        """Send a message and return its id, or None if it wasn't delivered to Telegram."""
        if not self.enabled:
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            links = "\n".join(f"  {label}: {url}" for row in buttons or [] for label, url in row)
            print(f"\n{'=' * 60}\n{plain}\n{links}\n{'=' * 60}", flush=True)
            return None
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_to:
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": label, "url": url} for label, url in row] for row in buttons]
            }
        try:
            result = await self._call("sendMessage", payload)
            return result.get("message_id")
        except Exception as exc:
            if "parse entities" not in str(exc):
                log.error("Failed to send Telegram message: %s", exc)
                return None
            # Broken HTML formatting: still deliver the content, as plain text.
            log.error("Bad message formatting, resending as plain text: %s", exc)
            payload.pop("parse_mode")
            payload["text"] = html.unescape(re.sub(r"<[^>]+>", "", text))
            try:
                result = await self._call("sendMessage", payload)
                return result.get("message_id")
            except Exception as retry_exc:
                log.error("Failed to send Telegram message: %s", retry_exc)
                return None

    async def member_count(self, username: str) -> int | None:
        """Member count of a public group or channel, or None if it can't be read."""
        if not self.enabled:
            return None
        try:
            return int(await self._call("getChatMemberCount", {"chat_id": f"@{username}"}))
        except Exception:
            return None

    async def updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        """Long-poll for new messages."""
        payload = {"offset": offset, "timeout": timeout, "allowed_updates": ["message"]}
        return await self._call("getUpdates", payload, http_timeout=timeout + 10)
