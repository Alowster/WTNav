"""Cliente asíncrono del servidor local de War Thunder (puerto 8111)."""
from __future__ import annotations

import json

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkReply, QNetworkRequest


class WTClient(QObject):
    stateReceived = Signal(dict)
    indicatorsReceived = Signal(dict)
    mapObjReceived = Signal(list)
    mapInfoReceived = Signal(dict)
    mapImageReceived = Signal(bytes)
    iconFontReceived = Signal(bytes)
    connectionChanged = Signal(bool)

    FAST_MS = 200

    def __init__(self, host: str = "127.0.0.1:8111", parent=None):
        super().__init__(parent)
        self.host = host
        self.nam = QNetworkAccessManager(self)
        self.nam.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))
        self._inflight: set[str] = set()
        self._fails = 0
        self._connected = False
        self._tick = 0
        self._generation = None
        self._font_tried = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll)

    def start(self):
        self.timer.start(self.FAST_MS)
        self._poll()

    def set_host(self, host: str):
        self.host = host.strip() or "127.0.0.1:8111"
        self._generation = None
        self._font_tried = False

    # -----------------------------------------------------------------
    def _poll(self):
        self._tick += 1
        self._get("/map_obj.json", lambda d: self.mapObjReceived.emit(d if isinstance(d, list) else []))
        self._get("/state", lambda d: self.stateReceived.emit(d if isinstance(d, dict) else {}))
        self._get("/indicators", lambda d: self.indicatorsReceived.emit(d if isinstance(d, dict) else {}))
        if self._tick % 5 == 1:
            self._get("/map_info.json", self._on_info)
        if not self._font_tried and self._connected:
            self._font_tried = True
            self._get("/icons.ttf", self.iconFontReceived.emit, raw=True)

    def _on_info(self, info):
        if not isinstance(info, dict):
            return
        self.mapInfoReceived.emit(info)
        gen = info.get("map_generation")
        if info.get("valid", True) and gen is not None and gen != self._generation:
            self._generation = gen
            self._get(f"/map.img?gen={gen}", self.mapImageReceived.emit, raw=True)

    def _get(self, path: str, handler, raw: bool = False):
        key = path.split("?")[0]
        if key in self._inflight:
            return
        self._inflight.add(key)
        req = QNetworkRequest(QUrl(f"http://{self.host}{path}"))
        req.setTransferTimeout(2000)
        reply = self.nam.get(req)
        reply.finished.connect(lambda r=reply, k=key, h=handler, rw=raw: self._done(r, k, h, rw))

    def _done(self, reply: QNetworkReply, key: str, handler, raw: bool):
        self._inflight.discard(key)
        try:
            if reply.error() != QNetworkReply.NoError:
                if key in ("/state", "/map_obj.json"):
                    self._fails += 1
                    if self._fails >= 5:
                        self._set_connected(False)
                return
            data = bytes(reply.readAll().data())
            if key in ("/state", "/map_obj.json"):
                self._fails = 0
                self._set_connected(True)
            if raw:
                if data:
                    handler(data)
                return
            try:
                parsed = json.loads(data.decode("utf-8", errors="replace")) if data else None
            except ValueError:
                return
            handler(parsed)
        finally:
            reply.deleteLater()

    def _set_connected(self, ok: bool):
        if ok != self._connected:
            self._connected = ok
            if not ok:
                self._generation = None
            self.connectionChanged.emit(ok)
