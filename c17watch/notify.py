"""Phone notifications. ntfy is the default (free, no account needed)."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


@dataclass
class Message:
    title: str
    body: str
    click_url: str | None = None
    priority: str = "high"  # low | default | high


class NotifyError(Exception):
    pass


class Notifier:
    def send(self, msg: Message) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def send_with_retry(self, msg: Message, attempts: int = 3) -> bool:
        for i in range(attempts):
            try:
                self.send(msg)
                return True
            except NotifyError as e:
                log.warning("notification failed (attempt %d/%d): %s", i + 1, attempts, e)
                if i + 1 < attempts:
                    time.sleep(2 ** (i + 1))
        return False


class ConsoleNotifier(Notifier):
    """Prints instead of sending. Used for --dry-run."""

    def send(self, msg: Message) -> None:
        print(f"\n[ALERT] {msg.title}\n{msg.body}\n{msg.click_url or ''}\n", flush=True)


class NtfyNotifier(Notifier):
    """https://ntfy.sh — install the ntfy app and subscribe to your topic.

    On the public ntfy.sh server anyone who knows the topic name can read it,
    so treat the topic like a password (long and random), or self-host ntfy
    with access control and set NTFY_TOKEN.
    """

    PRIORITY = {"low": 2, "default": 3, "high": 4}

    def __init__(self, server: str, topic: str, token: str | None, session: requests.Session):
        if not topic:
            raise ValueError("ntfy topic is empty; set NTFY_TOPIC or notify.ntfy_topic")
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.session = session

    def send(self, msg: Message) -> None:
        # JSON publishing handles non-ASCII titles, unlike header-based publishing.
        payload = {
            "topic": self.topic,
            "title": msg.title,
            "message": msg.body,
            "priority": self.PRIORITY.get(msg.priority, 3),
            "tags": ["airplane"],
        }
        if msg.click_url:
            payload["click"] = msg.click_url
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            r = self.session.post(self.server, json=payload, headers=headers, timeout=10)
        except requests.RequestException as e:
            raise NotifyError(str(e)) from e
        if r.status_code >= 400:
            raise NotifyError(f"ntfy HTTP {r.status_code}: {r.text[:200]}")


class PushoverNotifier(Notifier):
    """https://pushover.net — one-time app purchase, very reliable delivery."""

    URL = "https://api.pushover.net/1/messages.json"
    PRIORITY = {"low": -1, "default": 0, "high": 1}

    def __init__(self, app_token: str, user_key: str, session: requests.Session):
        if not app_token or not user_key:
            raise ValueError("set PUSHOVER_APP_TOKEN and PUSHOVER_USER_KEY")
        self.app_token, self.user_key, self.session = app_token, user_key, session

    def send(self, msg: Message) -> None:
        data = {
            "token": self.app_token,
            "user": self.user_key,
            "title": msg.title,
            "message": msg.body,
            "priority": self.PRIORITY.get(msg.priority, 0),
        }
        if msg.click_url:
            data["url"] = msg.click_url
        try:
            r = self.session.post(self.URL, data=data, timeout=10)
        except requests.RequestException as e:
            raise NotifyError(str(e)) from e
        if r.status_code >= 400:
            raise NotifyError(f"pushover HTTP {r.status_code}: {r.text[:200]}")


def build_notifier(cfg: dict, session: requests.Session, dry_run: bool) -> Notifier:
    if dry_run:
        return ConsoleNotifier()
    method = cfg.get("method", "ntfy").lower()
    if method == "console":
        return ConsoleNotifier()
    if method == "ntfy":
        return NtfyNotifier(
            server=cfg.get("ntfy_server", "https://ntfy.sh"),
            topic=os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic", ""),
            token=os.environ.get("NTFY_TOKEN"),
            session=session,
        )
    if method == "pushover":
        return PushoverNotifier(
            os.environ.get("PUSHOVER_APP_TOKEN", ""),
            os.environ.get("PUSHOVER_USER_KEY", ""),
            session,
        )
    raise ValueError(f"unknown notify method '{method}' (use ntfy, pushover or console)")
