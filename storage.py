"""
Простое персистентное хранилище на JSON-файле.
Для продакшена стоит заменить на настоящую БД (Postgres/Redis),
но для одного бота и умеренного числа игроков этого достаточно,
а Railway диск между деплоями не переживает — так что при желании
можно подключить Railway Volume, чтобы data.json не терялся.
"""

import json
import os
import threading
import time
import uuid
from typing import Optional

DATA_FILE = os.environ.get("DATA_FILE", "data.json")
_lock = threading.Lock()


def _empty_state() -> dict:
    return {
        "links": {},
        "users": {},
        "messages": {},
        "tg_to_game": {},
        "feed": [],
    }


class Storage:
    def __init__(self, path: str = DATA_FILE):
        self.path = path
        self.state = _empty_state()
        self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.state = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.state = _empty_state()
        defaults = _empty_state()
        for key, default_value in defaults.items():
            self.state.setdefault(key, default_value)

    def _save(self):
        tmp_path = self.path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    def create_link(self) -> tuple[str, str]:
        with _lock:
            game_user_id = str(uuid.uuid4())
            link_token = uuid.uuid4().hex[:16]
            self.state["links"][link_token] = {
                "game_user_id": game_user_id,
                "status": "pending",
                "telegram_id": None,
            }
            self.state["users"][game_user_id] = {
                "telegram_id": None,
                "profile": {},
                "blocked": False,
            }
            self.state["messages"].setdefault(game_user_id, [])
            self._save()
            return game_user_id, link_token

    def get_link(self, link_token: str) -> Optional[dict]:
        return self.state["links"].get(link_token)

    def confirm_link(self, link_token: str, telegram_id: int, profile: dict):
        with _lock:
            link = self.state["links"].get(link_token)
            if not link:
                return None
            game_user_id = link["game_user_id"]
            link["status"] = "linked"
            link["telegram_id"] = telegram_id
            self.state["users"][game_user_id] = {
                "telegram_id": telegram_id,
                "profile": profile,
                "blocked": False,
            }
            self.state["tg_to_game"][str(telegram_id)] = game_user_id
            self._save()
            return game_user_id

    def find_pending_link_by_telegram_id(self, telegram_id: int) -> Optional[str]:
        for token, link in self.state["links"].items():
            if link["telegram_id"] == telegram_id and link["status"] == "pending":
                return token
        return None

    def mark_pending_telegram_id(self, link_token: str, telegram_id: int):
        with _lock:
            link = self.state["links"].get(link_token)
            if link:
                link["telegram_id"] = telegram_id
                self._save()

    def get_user(self, game_user_id: str) -> Optional[dict]:
        return self.state["users"].get(game_user_id)

    def get_game_user_by_telegram_id(self, telegram_id: int) -> Optional[str]:
        return self.state["tg_to_game"].get(str(telegram_id))

    def set_blocked(self, game_user_id: str, blocked: bool) -> bool:
        with _lock:
            user = self.state["users"].get(game_user_id)
            if not user:
                return False
            user["blocked"] = blocked
            self._save()
            return True

    def display_name(self, game_user_id: str) -> str:
        user = self.state["users"].get(game_user_id, {})
        profile = user.get("profile", {})
        return (
            profile.get("username")
            or profile.get("first_name")
            or f"Player-{game_user_id[:6]}"
        )

    def add_message(self, game_user_id: str, direction: str, text: str) -> dict:
        with _lock:
            msg = {"direction": direction, "text": text, "ts": time.time()}
            self.state["messages"].setdefault(game_user_id, []).append(msg)
            feed_entry = {
                "game_user_id": game_user_id,
                "display_name": self.display_name(game_user_id),
                "direction": direction,
                "text": text,
                "ts": msg["ts"],
            }
            self.state["feed"].append(feed_entry)
            self._save()
            return msg

    def get_messages(self, game_user_id: str, since: float = 0.0, limit: int = 200) -> list:
        msgs = self.state["messages"].get(game_user_id, [])
        filtered = [m for m in msgs if m["ts"] > since]
        return filtered[-limit:]

    def get_feed(self, since: float = 0.0, limit: int = 200) -> list:
        feed = self.state.get("feed", [])
        filtered = [f for f in feed if f["ts"] > since]
        return filtered[-limit:]


storage = Storage()
