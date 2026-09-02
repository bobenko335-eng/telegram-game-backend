"""
Менеджер WebSocket-соединений. Один game_user_id может держать
несколько открытых вкладок/соединений — рассылаем всем.
"""

import asyncio
from typing import Dict, Set

from fastapi import WebSocket


class WSManager:
    def __init__(self):
        self._connections: Dict[str, Set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, game_user_id: str, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(game_user_id, set()).add(ws)

    async def disconnect(self, game_user_id: str, ws: WebSocket):
        async with self._lock:
            conns = self._connections.get(game_user_id)
            if conns and ws in conns:
                conns.remove(ws)
            if conns is not None and not conns:
                self._connections.pop(game_user_id, None)

    async def send(self, game_user_id: str, payload: dict):
        conns = list(self._connections.get(game_user_id, ()))
        dead = []
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.get(game_user_id, set()).discard(ws)


ws_manager = WSManager()
