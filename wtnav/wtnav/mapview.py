"""Vista del mapa: imagen del juego + capas calculadas (rastro, base, eje de pista, alcances...)."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF, QTransform)
from PySide6.QtWidgets import QWidget

from .calc import Snapshot, bearing, fmt_dist, split_trail, unit_from_bearing
from .filters import MapFilters, classify, classify_airfield
from .tracker import MEMORY_S, NEW_PULSE_S

ICON_CHARS = {
    "Airdefence": "4", "Structure": "5", "waypoint": "6", "capture_zone": "7",
    "bombing_point": "8", "defending_point": "9", "respawn_base_tank": "0",
    "respawn_base_fighter": ".", "respawn_base_bomber": ":",
}

# Colores de rastro por antigüedad: índice 0 = más reciente (blanco), 1,2,... = vidas anteriores
TRAIL_COLORS = [
    QColor(255, 255, 255, 120),
    QColor(100, 200, 255, 120),
    QColor(255, 200, 80,  120),
    QColor(100, 255, 150, 120),
    QColor(255, 120, 200, 120),
    QColor(220, 150, 255, 120),
    QColor(255, 255, 100, 120),
]


class MapWidget(QWidget):
    waypointSet = Signal(float, float)   # normalizado
    waypointCleared = Signal()
    homePicked = Signal(str)
    trailSegmentDeleted = Signal(int)    # índice del segmento eliminado
    radiusChanged = Signal(float)        # km, al cambiar la escala con la rueda en modo brújula

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(300, 300)
        self.setMouseTracking(True)
        self.pixmap: QPixmap | None = None
        self.info: dict = {}
        self.objs: list = []
        self.snap = Snapshot()
        self.scale = 1.0
        self.pan = QPointF(0, 0)
        self.follow = False
        self.heading_up = False
        # Filtros activos (el mapa cuadrado y la brújula tienen los suyos; main los intercambia)
        self.filters = MapFilters("square")
        # Modo brújula: círculo centrado en ti, rumbo arriba, radio fijado en metros
        self.circular = False
        self.radius_m = 20000.0
        self._win_drag = None
        self._has_pulse = False
        self.icon_family: str | None = None
        self._press = None
        self._dragging = False
        self._hover = None
        self._t0 = time.monotonic()
        # Fotograma cacheado: el mapa completo solo se repinta cuando cambian datos o vista.
        # El hover (etiqueta bajo el ratón) se dibuja encima sin repintar todo lo demás.
        self._frame: QPixmap | None = None
        self._frame_key = None
        self._has_blink = False
        self._blink_timer = QTimer(self)
        self._blink_timer.setInterval(50)
        self._blink_timer.timeout.connect(self.update)
        self._center()

    # ------------------------------------------------------------ filtros y modo
    # Compatibilidad: los antiguos interruptores ahora son ayudas de los filtros
    show_trail = property(lambda self: self.filters.helper("trail"),
                          lambda self, v: self.filters.helpers.__setitem__("trail", bool(v)))
    show_rings = property(lambda self: self.filters.helper("rings"),
                          lambda self, v: self.filters.helpers.__setitem__("rings", bool(v)))
    show_enemy_cones = property(lambda self: self.filters.helper("cones"),
                                lambda self, v: self.filters.helpers.__setitem__("cones", bool(v)))
    show_contacts = property(lambda self: self.filters.helper("contacts"),
                             lambda self, v: self.filters.helpers.__setitem__("contacts", bool(v)))

    @property
    def rotating(self) -> bool:
        """Rumbo arriba: elegido por el usuario o impuesto por el modo brújula."""
        return self.heading_up or self.circular

    def set_circular(self, on: bool):
        self.circular = on
        if on:
            self.setMinimumSize(120, 120)
            self.setCursor(Qt.SizeAllCursor)
        else:
            self.setMinimumSize(300, 300)
            self.unsetCursor()
            self.reset_view()
        self._hover = None
        self._apply_circle_view()
        self.update()

    def set_radius_km(self, km: float):
        self.radius_m = max(1000.0, min(200000.0, km * 1000.0))
        self._apply_circle_view()
        self.update()

    def set_filters(self, f: MapFilters):
        self.filters = f
        self.update()

    def _circle_r(self) -> float:
        return min(self.width(), self.height()) / 2.0 - 2.0

    def _circle_path(self) -> QPainterPath:
        path = QPainterPath()
        r = self._circle_r()
        path.addEllipse(QPointF(self.width() / 2.0, self.height() / 2.0), r, r)
        return path

    def _apply_circle_view(self):
        """Brújula: escala a partir del radio pedido y centrado en el jugador."""
        if not self.circular:
            return
        s = self.snap
        sx = s.map_size[0]
        if not sx or not self._base():
            return
        k = self._circle_r() * sx / self.radius_m      # px por unidad normalizada
        self.scale = k / self._base()
        if s.has_player:
            self.center_on_world(s.px, s.py)

    @staticmethod
    def _obj_side_cat(o: dict):
        side, cat = classify(o)
        return side, cat, str(o.get("icon_bg", "")).endswith("Target")

    def _obj_visible(self, o: dict) -> bool:
        side, cat, tgt = self._obj_side_cat(o)
        return self.filters.object_visible(side, cat) or (tgt and self.filters.special("target"))

    def _track_visible(self, t) -> bool:
        _, cat = classify({"type": "aircraft", "icon": t.icon})
        return (self.filters.object_visible("enemy", cat)
                or (t.is_target and self.filters.special("target")))

    # ------------------------------------------------------------ datos
    def set_image(self, data: bytes):
        pm = QPixmap()
        if pm.loadFromData(data):
            self.pixmap = pm
            self.reset_view()

    def set_icon_font(self, data: bytes):
        fid = QFontDatabase.addApplicationFontFromData(data)
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if fams:
            self.icon_family = fams[0]

    def set_data(self, info, objs, snap):
        self.info, self.objs, self.snap = info or {}, objs or [], snap
        if self.circular:
            self._apply_circle_view()
        elif self.follow and snap.has_player:
            self.center_on_world(snap.px, snap.py)
        # La animación (parpadeo, aviso de contacto nuevo) solo corre si hay algo que animar
        self._has_blink = any(o.get("blink") and self._obj_visible(o) for o in self.objs)
        self._has_pulse = self.show_contacts and any(
            snap.now - t.first_seen < NEW_PULSE_S for t in snap.tracks)
        animate = self._has_blink or self._has_pulse
        if animate and not self._blink_timer.isActive():
            self._blink_timer.start()
        elif not animate and self._blink_timer.isActive():
            self._blink_timer.stop()
        self.update()

    def clear_image(self):
        self.pixmap = None
        self.update()

    # ------------------------------------------------------------ transformaciones
    def _base(self):
        return float(min(self.width(), self.height()))

    def _center(self):
        b = self._base() * self.scale
        self.pan = QPointF((self.width() - b) / 2, (self.height() - b) / 2)

    def reset_view(self):
        self.scale = 1.0
        self._center()
        self.update()

    def n2s(self, nx, ny) -> QPointF:
        k = self._base() * self.scale
        return QPointF(self.pan.x() + nx * k, self.pan.y() + ny * k)

    def w2s(self, x, y) -> QPointF:
        sx, sy = self.snap.map_size
        if not sx or not sy:
            return QPointF(-1e6, -1e6)
        return self.n2s(x / sx, y / sy)

    def s2n(self, p: QPointF):
        k = self._base() * self.scale
        return (p.x() - self.pan.x()) / k, (p.y() - self.pan.y()) / k

    def _unrotate_screen(self, pos: QPointF) -> QPointF:
        """Convierte una posición de pantalla al espacio sin rotar cuando heading_up está activo."""
        s = self.snap
        if not self.rotating or not s.has_player or s.heading is None:
            return pos
        c = self.w2s(s.px, s.py)
        t = QTransform()
        t.translate(c.x(), c.y())
        t.rotate(s.heading)   # inversa de -heading
        t.translate(-c.x(), -c.y())
        return t.map(pos)

    def m2px(self, meters) -> float:
        sx = self.snap.map_size[0]
        return meters / sx * self._base() * self.scale if sx else 0.0

    def center_on_world(self, x, y):
        sx, sy = self.snap.map_size
        if not sx:
            return
        k = self._base() * self.scale
        self.pan = QPointF(self.width() / 2 - x / sx * k, self.height() / 2 - y / sy * k)

    def resizeEvent(self, e):
        if self.circular:
            self._apply_circle_view()
            super().resizeEvent(e)
            return
        if self.scale == 1.0:
            self._center()
        if self.follow and self.snap.has_player:
            self.center_on_world(self.snap.px, self.snap.py)
        super().resizeEvent(e)

    # ------------------------------------------------------------ ratón
    def wheelEvent(self, e):
        # Zoom anclado al cursor: el punto del mapa bajo el ratón no se mueve.
        # Se recoloca midiendo dónde acaba ese punto en pantalla (con la rotación incluida)
        # y desplazando la vista la diferencia; vale igual con norte arriba que con rumbo arriba.
        if self.circular:          # en la brújula la rueda cambia el radio
            f = 1.0015 ** (-e.angleDelta().y())
            self.set_radius_km(self.radius_m / 1000.0 * f)
            self.radiusChanged.emit(self.radius_m / 1000.0)
            return
        cursor = e.position()
        anchor = self.s2n(self._unrotate_screen(cursor))
        f = 1.0015 ** e.angleDelta().y()
        self.scale = max(1.0, min(20.0, self.scale * f))
        after = self._rot_transform().map(self.n2s(*anchor))
        self.pan += cursor - after
        if self.scale == 1.0:
            self._center()
        if self.follow and self.snap.has_player:
            self.center_on_world(self.snap.px, self.snap.py)
        self.update()

    def mousePressEvent(self, e):
        if self.circular:
            # La brújula no es interactiva: arrastrar en cualquier punto mueve la ventana
            if e.button() == Qt.LeftButton:
                wh = self.window().windowHandle()
                if wh is None or not wh.startSystemMove():
                    self._win_drag = e.globalPosition().toPoint() - self.window().frameGeometry().topLeft()
            return
        self._press = (e.position(), QPointF(self.pan), e.button())
        self._dragging = False

    def mouseMoveEvent(self, e):
        if self.circular:
            if self._win_drag is not None and e.buttons() & Qt.LeftButton:
                self.window().move(e.globalPosition().toPoint() - self._win_drag)
            return
        self._hover = e.position()
        if self._press and self._press[2] == Qt.LeftButton:
            d = e.position() - self._press[0]
            if self._dragging or abs(d.x()) + abs(d.y()) > 5:
                self._dragging = True
                self.follow = False
                self.pan = self._press[1] + d
        self.update()

    def mouseReleaseEvent(self, e):
        self._win_drag = None
        if self.circular:
            return
        if self._press and not self._dragging:
            pos = self._unrotate_screen(e.position())
            if e.button() == Qt.LeftButton:
                seg = self._hit_trail(pos)
                if seg >= 0:
                    self.trailSegmentDeleted.emit(seg)
                else:
                    nx, ny = self.s2n(pos)
                    if 0 <= nx <= 1 and 0 <= ny <= 1:
                        self.waypointSet.emit(nx, ny)
            elif e.button() == Qt.RightButton:
                self.waypointCleared.emit()
        self._press = None
        self._dragging = False

    def mouseDoubleClickEvent(self, e):
        if self.circular:
            return
        pos = self._unrotate_screen(e.position())
        best, bd = None, 25.0
        for af in self.snap.airfields:
            c = self.w2s(af.cx, af.cy)
            d = math.hypot(c.x() - pos.x(), c.y() - pos.y())
            if d < bd:
                best, bd = af, d
        if best:
            self.homePicked.emit(best.key)
            self.waypointCleared.emit()

    def leaveEvent(self, e):
        self._hover = None
        self.update()

    # ------------------------------------------------------------ pintado
    def _view_key(self):
        """Todo lo que cambia el aspecto del mapa. Si no cambia, se reutiliza el fotograma."""
        return (self.width(), self.height(), self.devicePixelRatioF(), self.scale,
                round(self.pan.x(), 2), round(self.pan.y(), 2), id(self.snap), id(self.pixmap),
                self.icon_family, self.heading_up, self.circular, self.radius_m,
                self.filters.signature())

    def paintEvent(self, e):
        key = self._view_key()
        if key != self._frame_key or self._frame is None:
            self._render_frame()
            self._frame_key = key
        p = QPainter(self)
        p.drawPixmap(0, 0, self._frame)
        s = self.snap
        if s.in_game:
            p.setRenderHint(QPainter.Antialiasing)
            if self._has_blink or self._has_pulse:
                # encima del fotograma: animarlos no obliga a repintar el mapa
                p.save()
                if self.circular:
                    p.setClipPath(self._circle_path())
                p.setTransform(self._rot_transform(), True)
                if self._has_blink:
                    self._draw_objects(p, blinking=True)
                if self._has_pulse:
                    self._draw_new_contact_pulses(p, s)
                p.restore()
            if not self.circular:
                self._draw_hover(p, s)

    def _rot_transform(self) -> QTransform:
        s, t = self.snap, QTransform()
        if self.rotating and s.in_game and s.has_player and s.heading is not None:
            c = self.w2s(s.px, s.py)
            t.translate(c.x(), c.y())
            t.rotate(-s.heading)
            t.translate(-c.x(), -c.y())
        return t

    def _render_frame(self):
        dpr = self.devicePixelRatioF()
        frame = QPixmap(max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr)))
        frame.setDevicePixelRatio(dpr)
        # Brújula: fuera del círculo todo transparente (la ventana es circular sobre el juego)
        frame.fill(Qt.transparent if self.circular else QColor("#1b1f1d"))
        p = QPainter(frame)
        p.setRenderHint(QPainter.Antialiasing)
        s = self.snap
        F = self.filters

        if self.circular:
            p.setClipPath(self._circle_path())
            p.fillRect(self.rect(), QColor("#1b1f1d"))

        k = self._base() * self.scale
        map_rect = QRectF(self.pan.x(), self.pan.y(), k, k)

        rot = self._rot_transform()
        _rotated = not rot.isIdentity()
        if _rotated:
            p.save()
            p.setTransform(rot, True)

        if self.pixmap:
            self._draw_map_image(p, map_rect, rot)
        else:
            p.fillRect(map_rect, QColor("#2a302c"))

        if s.in_game:
            if F.helper("grid"):
                self._draw_grid(p)
            if F.helper("rings"):
                self._draw_rings(p, s, s.px, s.py)
            if F.helper("cones"):
                self._draw_enemy_cones(p, s)
            if F.helper("contacts") and s.has_player:
                self._draw_contacts(p, s)
            self._draw_airfields(p, s)
            if F.helper("trail"):
                self._draw_trail(p, s)
            self._draw_objects(p, blinking=False)
            self._draw_nav_lines(p, s, s.px, s.py)
            if s.has_player:
                self._draw_player(p, s, s.px, s.py)

        if _rotated:
            p.restore()

        if self.circular:
            p.setClipping(False)
            if s.in_game and s.has_player:
                self._draw_rim_markers(p, s)
            self._draw_compass_ring(p, s)
            if not s.in_game:
                p.setPen(QColor("#aaa"))
                p.setFont(QFont("Segoe UI", 9))
                p.drawText(self.rect(), Qt.AlignCenter, "Esperando\npartida...")
            else:
                self._draw_alert_icons(p, s, compact=True)
        elif not s.in_game:
            p.setPen(QColor("#aaa"))
            p.setFont(QFont("Segoe UI", 14))
            p.drawText(self.rect(), Qt.AlignCenter, "Esperando partida...\n(localhost:8111)")
        else:
            self._draw_scale(p)
            self._draw_wind(p, s)
            self._draw_alert_icons(p, s)
            if self.heading_up and s.has_player:
                self._draw_north_indicator(p, s)
        p.end()
        self._frame = frame

    def _draw_map_image(self, p: QPainter, map_rect: QRectF, rot: QTransform):
        """Escala solo la parte visible de la imagen: con mucho zoom ahorra la mayor parte del trabajo."""
        inv, ok = rot.inverted()
        visible = (inv.mapRect(QRectF(self.rect())) if ok else QRectF(self.rect())).intersected(map_rect)
        if visible.isEmpty():
            return
        pw, ph = self.pixmap.width(), self.pixmap.height()
        fx, fy = pw / map_rect.width(), ph / map_rect.height()
        src = QRectF((visible.x() - map_rect.x()) * fx, (visible.y() - map_rect.y()) * fy,
                     visible.width() * fx, visible.height() * fy)
        p.drawPixmap(visible, self.pixmap, src)

    def _blink(self, kind):
        t = time.monotonic() - self._t0
        period = 1.2 if kind == 2 else 2.0
        tt = t % period
        return math.exp(-((5 * tt - 2) ** 4))

    @staticmethod
    def grid_geometry(info: dict):
        """Cuadrícula del juego en coordenadas normalizadas (0..1, y hacia abajo = sur).

        La cuadrícula A-1 no siempre cubre el mapa entero: empieza en grid_zero (esquina
        superior izquierda, en metros, con y hacia el NORTE) y mide grid_size. Comprobado con
        dos mapas reales: uno con cuadrícula parcial (52,7 x 55,4 km dentro de 131 km) y otro
        donde cubre el mapa completo. Devuelve None si faltan datos.
        """
        try:
            mn, mx, st = info["map_min"], info["map_max"], info["grid_steps"]
        except (KeyError, TypeError):
            return None
        sx, sy = mx[0] - mn[0], mx[1] - mn[1]
        if not sx or not sy or not st[0] or not st[1]:
            return None
        gz, gs = info.get("grid_zero"), info.get("grid_size")
        if gz and gs and gs[0] > 0 and gs[1] > 0:
            x0, y0 = (gz[0] - mn[0]) / sx, (mx[1] - gz[1]) / sy
            w, h = gs[0] / sx, gs[1] / sy
        else:                                   # sin datos de cuadrícula: el mapa entero
            x0, y0, w, h = 0.0, 0.0, 1.0, 1.0
        dx, dy = st[0] / sx, st[1] / sy
        # líneas cada paso, más el borde final si la última casilla es parcial
        xs = [x0 + i * dx for i in range(int(w / dx + 1e-6) + 1)]
        ys = [y0 + j * dy for j in range(int(h / dy + 1e-6) + 1)]
        if x0 + w - xs[-1] > 1e-6:
            xs.append(x0 + w)
        if y0 + h - ys[-1] > 1e-6:
            ys.append(y0 + h)
        # se rotulan las casillas cuyo centro cae dentro de la cuadrícula
        cols = [x0 + (i + 0.5) * dx for i in range(int(w / dx + 0.5))]
        rows = [y0 + (j + 0.5) * dy for j in range(min(26, int(h / dy + 0.5)))]
        return {"x0": x0, "y0": y0, "x1": x0 + w, "y1": y0 + h, "xs": xs, "ys": ys,
                "cols": cols, "rows": rows}

    def _draw_grid(self, p):
        g = self.grid_geometry(self.info)
        if not g:
            return
        p.setPen(QPen(QColor(20, 20, 20, 110), 1))
        for x in g["xs"]:
            p.drawLine(self.n2s(x, g["y0"]), self.n2s(x, g["y1"]))
        for y in g["ys"]:
            p.drawLine(self.n2s(g["x0"], y), self.n2s(g["x1"], y))
        p.setPen(QColor(15, 15, 15, 220))
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        top = max(self.n2s(0, g["y0"]).y(), 0)         # rótulos pegados al borde visible
        left = max(self.n2s(g["x0"], 0).x(), 0)
        for i, cx in enumerate(g["cols"]):
            c = self.n2s(cx, g["y0"])
            if 0 < c.x() < self.width():
                self._text_upright(p, QPointF(c.x() - 4, top + 12), str(i + 1))
        for j, cy in enumerate(g["rows"]):
            c = self.n2s(g["x0"], cy)
            if 0 < c.y() < self.height():
                self._text_upright(p, QPointF(left + 3, c.y() + 4), chr(65 + j))

    def _draw_rings(self, p, s, px, py):
        if not s.has_player:
            return
        c = self.w2s(px, py)
        if s.range_m and s.range_m < s.map_size[0] * 3:
            r = self.m2px(s.range_m)
            p.setPen(QPen(QColor(90, 200, 255, 150), 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)
            self._text_upright(p, c + QPointF(r * 0.7071 + 3, -r * 0.7071), "autonomía")
        if s.glide_reach and s.glide_ratio:
            c = self.w2s(s.glide_cx, s.glide_cy)
            r = self.m2px(s.glide_reach)
            col = QColor(255, 170, 60, 200 if s.glide_live else 90)
            p.setPen(QPen(col, 2 if s.glide_live else 1, Qt.DotLine))
            p.drawEllipse(c, r, r)
            self._text_upright(p, c + QPointF(-r * 0.7071 - 40, r * 0.7071 + 12), "planeo")

    def _draw_airfields(self, p, s):
        F = self.filters
        for af in s.airfields:
            side, cat = classify_airfield(af)
            is_home = s.home is not None and af.key == s.home.key
            if not F.object_visible(side, cat) and not (is_home and F.special("home")):
                continue
            col = QColor(af.color) if QColor(af.color).isValid() else QColor("#888")
            is_home = is_home and F.special("home")
            p.setPen(QPen(col, (4 if is_home else 3) * math.sqrt(self.scale), Qt.SolidLine, Qt.RoundCap))
            p.drawLine(self.w2s(af.ax, af.ay), self.w2s(af.bx, af.by))
            if is_home:
                c = self.w2s(af.cx, af.cy)
                p.setPen(QPen(QColor("#7CFC9A"), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, 14, 14)
        if s.home and s.rwy_hdg is not None and F.helper("approach"):
            ux, uy = unit_from_bearing(s.rwy_hdg)
            thr = self.w2s(s.thr_x, s.thr_y)
            far = self.w2s(s.thr_x - ux * 12000, s.thr_y - uy * 12000)
            p.setPen(QPen(QColor(124, 252, 154, 170), 1.2, Qt.DashLine))
            p.drawLine(thr, far)
            p.setPen(QPen(QColor(124, 252, 154, 200), 1))
            for km in (2, 4, 6, 8, 10):
                t = self.w2s(s.thr_x - ux * km * 1000, s.thr_y - uy * km * 1000)
                nx, ny = -uy * 5, ux * 5
                p.drawLine(t + QPointF(nx, ny), t - QPointF(nx, ny))
            e = self.w2s(s.entry_x, s.entry_y)
            p.setBrush(QColor(124, 252, 154, 160))
            p.drawEllipse(e, 4, 4)
            self._text_upright(p, e + QPointF(6, -6), "IAF")

    def _draw_trail(self, p, s):
        segments = self._split_trail(s.trail)
        n = len(segments)
        p.setBrush(Qt.NoBrush)
        for i, seg in enumerate(segments):
            age = n - 1 - i          # 0 = más reciente, n-1 = más antiguo
            col = TRAIL_COLORS[age % len(TRAIL_COLORS)]
            path = QPainterPath()
            started = False
            for pt in seg:
                q = self.w2s(*pt)
                if not started:
                    path.moveTo(q)
                    started = True
                else:
                    path.lineTo(q)
            p.setPen(QPen(col, 1.5))
            p.drawPath(path)

    @staticmethod
    def _split_trail(trail: list) -> list[list]:
        return split_trail(trail)

    @staticmethod
    def _dist_point_segment(px, py, ax, ay, bx, by) -> float:
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def _hit_trail(self, pos: QPointF, threshold: float = 6.0) -> int:
        """Devuelve el índice del segmento de rastro más cercano al click, o -1."""
        segments = self._split_trail(self.snap.trail)
        best_d, best_idx = threshold, -1
        for seg_idx, seg in enumerate(segments):
            for i in range(len(seg)):
                a = self.w2s(*seg[i])
                if i + 1 < len(seg):
                    b = self.w2s(*seg[i + 1])
                    d = self._dist_point_segment(pos.x(), pos.y(),
                                                 a.x(), a.y(), b.x(), b.y())
                else:
                    d = math.hypot(pos.x() - a.x(), pos.y() - a.y())
                if d < best_d:
                    best_d, best_idx = d, seg_idx
        return best_idx

    def _draw_objects(self, p, blinking: bool | None = None):
        """blinking=None: todos · False: solo fijos (van al fotograma) · True: solo los que parpadean."""
        if self.icon_family:
            f = QFont(self.icon_family, 15)
            f.setBold(True)
        else:
            f = QFont("Segoe UI", 9, QFont.Bold)
        p.setFont(f)
        for o in self.objs:
            if o.get("type") == "airfield" or o.get("icon") == "Player" or "x" not in o:
                continue
            if blinking is not None and bool(o.get("blink")) != blinking:
                continue
            if not self._obj_visible(o):
                continue
            col = QColor(o.get("color", "#ccc"))
            if o.get("blink"):
                bv = self._blink(o["blink"])
                c0 = col
                col = QColor(int(c0.red() + (255 - c0.red()) * bv), int(c0.green() + (255 - c0.green()) * bv),
                             int(c0.blue() * (1 - bv)))
            c = self.n2s(o["x"], o["y"])
            icon = o.get("icon", "?")
            if self.icon_family:
                ch = ICON_CHARS.get(icon, icon[:1] or "?")
                p.save()
                p.translate(c)
                if o.get("type") in ("respawn_base_fighter", "respawn_base_bomber"):
                    p.rotate(math.degrees(math.atan2(o.get("dx", 0), -o.get("dy", 1))))
                path = QPainterPath()
                fm = p.fontMetrics()
                path.addText(-fm.horizontalAdvance(ch) / 2, fm.ascent() / 2 - 2, f, ch)
                p.setPen(QPen(QColor("#000"), 1))
                p.setBrush(col)
                p.drawPath(path)
                p.restore()
            else:
                p.setPen(QPen(QColor("#000"), 1))
                p.setBrush(col)
                if o.get("type") == "aircraft":
                    p.drawPolygon(QPolygonF([c + QPointF(0, -6), c + QPointF(5, 5), c + QPointF(-5, 5)]))
                else:
                    p.drawRect(QRectF(c.x() - 5, c.y() - 5, 10, 10))
                    p.setPen(QColor("#000"))
                    p.drawText(QRectF(c.x() - 5, c.y() - 6, 10, 12), Qt.AlignCenter, icon[:1])

    def _label(self, p, at: QPointF, text, col, map_space=False):
        fm = p.fontMetrics()
        w, h = fm.horizontalAdvance(text) + 8, fm.height() + 2
        rotated = map_space and self.rotating and self.snap.heading is not None
        if rotated:
            p.save()
            p.translate(at)
            p.rotate(self.snap.heading)
            r = QRectF(0, 0, w, h)
        else:
            r = QRectF(at.x(), at.y(), w, h)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawRoundedRect(r, 3, 3)
        p.setPen(col)
        p.drawText(r, Qt.AlignCenter, text)
        if rotated:
            p.restore()

    def _text_upright(self, p: QPainter, at: QPointF, text: str):
        """Dibuja texto compensando la rotación del mapa para que siempre sea legible."""
        if self.rotating and self.snap.heading is not None:
            p.save()
            p.translate(at)
            p.rotate(self.snap.heading)
            p.drawText(QPointF(0, 0), text)
            p.restore()
        else:
            p.drawText(at, text)

    def _draw_nav_lines(self, p, s, px, py):
        if not s.has_player:
            return
        me = self.w2s(px, py)
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        if s.home and self.filters.special("home"):
            h = self.w2s(s.home.cx, s.home.cy)
            p.setPen(QPen(QColor(124, 252, 154, 190), 1.5, Qt.DashLine))
            p.drawLine(me, h)
            self._label(p, h + QPointF(16, 10), f"BASE {fmt_dist(s.home_dist)} · {s.home_brg:03.0f}°", QColor("#7CFC9A"), map_space=True)
        if s.wp and self.filters.special("waypoint"):
            w = self.w2s(*s.wp)
            p.setPen(QPen(QColor("#FFD24A"), 1.5))
            p.drawLine(me, w)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(w, 7, 7)
            p.drawLine(w + QPointF(-11, 0), w + QPointF(11, 0))
            p.drawLine(w + QPointF(0, -11), w + QPointF(0, 11))
            if s.bomb_release and s.wp_brg is not None:
                ux, uy = unit_from_bearing(s.wp_brg)
                rel = self.w2s(s.wp[0] - ux * s.bomb_release, s.wp[1] - uy * s.bomb_release)
                p.setPen(QPen(QColor(255, 120, 80), 2))
                nx, ny = -uy * 7, ux * 7
                p.drawLine(rel + QPointF(nx, ny), rel - QPointF(nx, ny))
            self._label(p, w + QPointF(10, 6), f"{fmt_dist(s.wp_dist)} · {s.wp_brg:03.0f}°", QColor("#FFD24A"), map_space=True)

    def _draw_player(self, p, s, px, py):
        c = self.w2s(px, py)
        hdg = s.heading if s.heading is not None else 0.0
        p.save()
        p.translate(c)
        p.rotate(hdg)
        poly = QPolygonF([QPointF(0, -14), QPointF(7, 9), QPointF(0, 4), QPointF(-7, 9)])
        p.setPen(QPen(QColor("#222"), 2))
        p.setBrush(QColor("#fff"))
        p.drawPolygon(poly)
        p.restore()
        if s.track is not None and s.gs:
            # vector de 60 s de vuelo sobre el suelo
            ux, uy = unit_from_bearing(s.track)
            end = self.w2s(px + ux * s.gs * 60, py + uy * s.gs * 60)
            p.setPen(QPen(QColor(255, 255, 255, 160), 1, Qt.DotLine))
            p.drawLine(c, end)

    def _draw_scale(self, p):
        sx = self.snap.map_size[0]
        if not sx:
            return
        mpp = sx / (self._base() * self.scale)
        target = mpp * 120
        nice = 10 ** math.floor(math.log10(target))
        for m in (1, 2, 5, 10):
            if nice * m >= target:
                nice *= m
                break
        px = nice / mpp
        x0, y0 = 12, self.height() - 16
        p.setPen(QPen(QColor("#eee"), 2))
        p.drawLine(QPointF(x0, y0), QPointF(x0 + px, y0))
        p.drawLine(QPointF(x0, y0 - 4), QPointF(x0, y0 + 4))
        p.drawLine(QPointF(x0 + px, y0 - 4), QPointF(x0 + px, y0 + 4))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QPointF(x0, y0 - 6), fmt_dist(nice))

    def _draw_wind(self, p, s):
        if s.wind_speed is None or s.wind_speed < 1.5:
            return
        c = QPointF(self.width() - 52, 44)
        p.setPen(QPen(QColor(0, 0, 0, 0)))
        p.setBrush(QColor(0, 0, 0, 140))
        p.drawEllipse(c, 30, 30)
        p.save()
        p.translate(c)
        rot = s.wind_from + 180         # flecha hacia donde sopla
        if self.rotating and s.has_player and s.heading is not None:
            rot -= s.heading            # el mapa está girado: la flecha también
        p.rotate(rot)
        p.setPen(QPen(QColor("#9fd7ff"), 2))
        p.drawLine(QPointF(0, 16), QPointF(0, -16))
        p.drawLine(QPointF(0, -16), QPointF(-5, -9))
        p.drawLine(QPointF(0, -16), QPointF(5, -9))
        p.restore()
        p.setPen(QColor("#9fd7ff"))
        p.setFont(QFont("Segoe UI", 8))
        p.drawText(QRectF(c.x() - 45, c.y() + 30, 90, 14), Qt.AlignCenter,
                   f"viento {s.wind_speed * 3.6:.0f} km/h")

    def _draw_hover(self, p, s):
        if self._hover is None or not s.has_player or self._dragging:
            return
        nx, ny = self.s2n(self._unrotate_screen(self._hover))
        if not (0 <= nx <= 1 and 0 <= ny <= 1):
            return
        x, y = nx * s.map_size[0], ny * s.map_size[1]
        d = math.hypot(x - s.px, y - s.py)
        b = bearing(x - s.px, y - s.py)
        p.setFont(QFont("Segoe UI", 8))
        self._label(p, self._hover + QPointF(14, 14), f"{fmt_dist(d)} · {b:03.0f}°", QColor("#ddd"))

    # ------------------------------------------------------------------ iconos de alerta

    def _draw_alert_icons(self, p: QPainter, s: Snapshot, compact: bool = False):
        """Iconos de alerta flotando sobre el mapa, sin fondo sólido."""
        if not s.alerts:
            return

        SZ       = 46    # tamaño del bounding-box de cada icono
        LABEL_H  = 13    # altura del texto bajo el icono
        GAP      = 12    # separación entre iconos
        BOT      = 20    # margen desde el borde inferior
        if compact:      # brújula: más pequeños, dentro del círculo y por encima del borde
            SZ = max(18, int(self._circle_r() * 0.16))
            GAP = 6
            BOT = int(self.height() / 2 - self._circle_r() * 0.62)

        # Clasificar cada alerta
        items = []
        for lvl, text in s.alerts:
            col = QColor("#ff4c3a") if lvl == "crit" else QColor("#ffd040")
            tl  = text.lower()
            if "fuel" in tl or "combustible" in tl:
                items.append((col, "_ai_fuel",  "FUEL"))
            elif "agua" in tl:
                items.append((col, "_ai_thermo", "AGUA"))
            elif "aceite" in tl:
                items.append((col, "_ai_thermo", "ACEITE"))
            elif "planeando" in tl:
                items.append((col, "_ai_glide",  "PLANEO"))
            else:
                items.append((col, "_ai_warn",   "AVISO"))

        n = len(items)
        total_w = n * SZ + (n - 1) * GAP
        x0 = (self.width() - total_w) // 2
        y0 = self.height() - SZ - LABEL_H - BOT

        p.save()
        p.setRenderHint(QPainter.Antialiasing)

        lf = QFont("Segoe UI", 7, QFont.Bold)
        p.setFont(lf)

        for i, (col, fn, label) in enumerate(items):
            ix = x0 + i * (SZ + GAP)
            getattr(self, fn)(p, ix, y0, SZ, col)
            # Texto con sombra para legibilidad sobre cualquier fondo de mapa
            lr = QRectF(ix, y0 + SZ + 1, SZ, LABEL_H)
            p.setPen(QColor(0, 0, 0, 200))
            p.drawText(lr.translated(0, 1), Qt.AlignHCenter | Qt.AlignTop, label)
            p.setPen(col)
            p.drawText(lr, Qt.AlignHCenter | Qt.AlignTop, label)

        p.restore()

    # ── helpers de iconos ────────────────────────────────────────────────

    @staticmethod
    def _ai_shadow(p: QPainter, path: QPainterPath):
        shadow = QTransform().translate(1.5, 2.0).map(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 160))
        p.drawPath(shadow)

    def _ai_fuel(self, p: QPainter, x, y, sz, col):
        """Gota de combustible (teardrop apuntando hacia abajo)."""
        cx   = x + sz * 0.5
        r    = sz * 0.28
        bcy  = y + sz * 0.66   # centro del círculo inferior

        path = QPainterPath()
        path.moveTo(cx, y + sz * 0.06)
        path.cubicTo(cx + r, y + sz * 0.28, cx + r, bcy - r * 0.6, cx + r, bcy)
        path.arcTo(cx - r, bcy - r, r * 2, r * 2, 0, 180)
        path.cubicTo(cx - r, bcy - r * 0.6, cx - r, y + sz * 0.28, cx, y + sz * 0.06)
        path.closeSubpath()

        self._ai_shadow(p, path)
        p.setPen(QPen(QColor(0, 0, 0, 80), 1.5))
        p.setBrush(col)
        p.drawPath(path)

    def _ai_thermo(self, p: QPainter, x, y, sz, col):
        """Termómetro (tubo + bulbo)."""
        cx   = x + sz * 0.5
        tw   = sz * 0.18    # ancho del tubo
        bcy  = y + sz * 0.80
        br   = sz * 0.22    # radio del bulbo
        tt   = y + sz * 0.06

        path = QPainterPath()
        path.addRoundedRect(QRectF(cx - tw / 2, tt, tw, bcy - tt - br * 0.35), tw / 2, tw / 2)
        path.addEllipse(QPointF(cx, bcy), br, br)

        self._ai_shadow(p, path)
        p.setPen(QPen(QColor(0, 0, 0, 80), 1.5))
        p.setBrush(col)
        p.drawPath(path)

    def _ai_glide(self, p: QPainter, x, y, sz, col):
        """Flecha diagonal hacia abajo-derecha: no llego a base planeando."""
        ax1, ay1 = x + sz * 0.14, y + sz * 0.14
        ax2, ay2 = x + sz * 0.84, y + sz * 0.84
        hw       = sz * 0.28   # longitud de las alas de la flecha

        # Grosor basado en sz
        thick = max(2.5, sz * 0.13)

        dark = QPen(QColor(0, 0, 0, 170), thick + 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        main = QPen(col, thick, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)

        for pen in (dark, main):
            off = 1.5 if pen is dark else 0.0
            p.setPen(pen)
            p.drawLine(QPointF(ax1 + off, ay1 + off), QPointF(ax2 + off, ay2 + off))
            p.drawLine(QPointF(ax2 + off, ay2 + off), QPointF(ax2 + off - hw, ay2 + off))
            p.drawLine(QPointF(ax2 + off, ay2 + off), QPointF(ax2 + off, ay2 + off - hw))

    def _ai_warn(self, p: QPainter, x, y, sz, col):
        """Triángulo de aviso con signo de exclamación."""
        path = QPainterPath()
        path.moveTo(x + sz * 0.5,  y + sz * 0.04)
        path.lineTo(x + sz * 0.95, y + sz * 0.90)
        path.lineTo(x + sz * 0.05, y + sz * 0.90)
        path.closeSubpath()

        self._ai_shadow(p, path)
        p.setPen(QPen(QColor(0, 0, 0, 80), 1.5))
        p.setBrush(col)
        p.drawPath(path)

        # Signo "!"
        exc_w = sz * 0.13
        exc_x = x + sz * 0.5
        exc_top = y + sz * 0.26
        exc_path = QPainterPath()
        exc_path.addRoundedRect(
            QRectF(exc_x - exc_w / 2, exc_top, exc_w, sz * 0.36), exc_w / 2, exc_w / 2
        )
        exc_path.addEllipse(QPointF(exc_x, exc_top + sz * 0.44), exc_w / 2, exc_w / 2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 220))
        p.drawPath(exc_path)

    # ------------------------------------------------------------------ indicador de norte

    def _draw_north_indicator(self, p: QPainter, s: Snapshot):
        """Mini brújula en esquina superior izquierda, visible solo en modo Rumbo Arriba."""
        if s.heading is None:
            return
        CX, CY, R = 38, 38, 22
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        # Fondo circular semitransparente
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawEllipse(QPointF(CX, CY), R, R)
        # Flecha girada igual que el mapa: punta roja apunta al norte
        p.translate(CX, CY)
        p.rotate(-s.heading)
        p.setBrush(QColor("#e04040"))
        p.drawPolygon(QPolygonF([QPointF(0, -(R - 3)), QPointF(4, 2), QPointF(-4, 2)]))
        p.setBrush(QColor("#cccccc"))
        p.drawPolygon(QPolygonF([QPointF(0, R - 3), QPointF(4, -2), QPointF(-4, -2)]))
        p.restore()
        # Borde
        p.setPen(QPen(QColor("#555"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(CX, CY), R, R)
        # Etiqueta "N" junto a la punta roja
        hdg_rad = math.radians(s.heading)
        nx = CX - math.sin(hdg_rad) * (R + 8)
        ny = CY - math.cos(hdg_rad) * (R + 8)
        p.setFont(QFont("Segoe UI", 7, QFont.Bold))
        p.setPen(QColor("#e04040"))
        p.drawText(QRectF(nx - 6, ny - 6, 12, 12), Qt.AlignCenter, "N")

    # ------------------------------------------------------------------ conos de enemigos

    @staticmethod
    def _fade(s: Snapshot, t) -> float:
        """1 mientras se ve; al perderlo se desvanece a lo largo de la memoria."""
        lost = t.lost_for(s.now)
        if lost <= 0.6:
            return 1.0
        return max(0.12, 1.0 - lost / MEMORY_S)

    def _draw_enemy_cones(self, p: QPainter, s: Snapshot):
        """Cono rojo translúcido: zona por la que puede pasar cada enemigo en los próximos segundos."""
        for t in s.tracks:
            if not self._track_visible(t):
                continue
            c = t.cone(s.now)
            if not c:
                continue
            f = self._fade(s, t)
            lost = not t.visible(s.now)
            pts = [self.w2s(x, y) for x, y in c["poly"]]
            start, end = self.w2s(t.x, t.y), self.w2s(*c["center"][-1])
            g = QLinearGradient(start, end)            # intenso cerca del avión, se diluye al final
            strong = 1.6 if t.is_target else 1.0
            g.setColorAt(0.0, QColor(255, 55, 45, int(85 * f * strong)))
            g.setColorAt(1.0, QColor(255, 55, 45, int(12 * f)))
            p.setBrush(QBrush(g))
            pen = QPen(QColor(255, 70, 60, int((190 if t.is_target else 120) * f)),
                       1.8 if t.is_target else 1.0, Qt.DashLine if lost else Qt.SolidLine)
            p.setPen(pen)
            p.drawPolygon(QPolygonF(pts), Qt.WindingFill)   # sin agujeros aunque los bordes se crucen
            # trayectoria más probable (giro actual) y marcas cada 5 s
            path = QPainterPath(self.w2s(*c["center"][0]))
            for q in c["center"][1:]:
                path.lineTo(self.w2s(*q))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 120, 110, int(150 * f)), 1, Qt.DotLine))
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 120, 110, int(170 * f)))
            p.setFont(QFont("Segoe UI", 7))
            for sec, q in c["ticks"]:
                qs = self.w2s(*q)
                p.drawEllipse(qs, 2.2, 2.2)
                if t.is_target:          # en tu blanco fijado: cuánto tarda en llegar ahí
                    p.setPen(QColor(255, 160, 150, int(220 * f)))
                    self._text_upright(p, qs + QPointF(4, -3), f"{sec}s")
                    p.setPen(Qt.NoPen)

    def _draw_contacts(self, p: QPainter, s: Snapshot):
        """Cono naranja desde ti hacia cada contacto, visible o recordado (se desvanece)."""
        me = self.w2s(s.px, s.py)
        p.setFont(QFont("Segoe UI", 8, QFont.Bold))
        for t in s.tracks:
            if not self._track_visible(t):
                continue
            d = math.hypot(t.x - s.px, t.y - s.py)
            if d < 50:
                continue
            f = self._fade(s, t)
            lost = not t.visible(s.now)
            ux, uy = (t.x - s.px) / d, (t.y - s.py) / d
            half = max(250.0, d * 0.035)               # ~2° de apertura, mínimo 250 m en el blanco
            a = self.w2s(t.x - uy * half, t.y + ux * half)
            b = self.w2s(t.x + uy * half, t.y - ux * half)
            tip = self.w2s(t.x, t.y)
            g = QLinearGradient(me, tip)
            g.setColorAt(0.0, QColor(255, 160, 40, int(8 * f)))
            g.setColorAt(1.0, QColor(255, 160, 40, int(75 * f)))
            p.setBrush(QBrush(g))
            p.setPen(QPen(QColor(255, 170, 60, int(110 * f)), 1, Qt.DashLine if lost else Qt.SolidLine))
            p.drawPolygon(QPolygonF([me, a, b]))
            if lost:                                   # última posición conocida
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor(255, 170, 60, int(200 * f)), 1.5))
                p.drawEllipse(tip, 6, 6)
            txt = fmt_dist(d)
            if lost:
                txt += f" · hace {t.lost_for(s.now):.0f} s"
            col = QColor(255, 190, 110, int(255 * max(f, 0.45)))
            self._label(p, tip + QPointF(8, 4), txt, col, map_space=True)

    def _draw_new_contact_pulses(self, p: QPainter, s: Snapshot):
        """Aviso de contacto nuevo: anillo naranja que se expande, para que no pase desapercibido."""
        now = time.monotonic()
        for t in s.tracks:
            age = now - t.first_seen
            if age >= NEW_PULSE_S or not self._track_visible(t):
                continue
            k = (age % 1.25) / 1.25                    # dos pulsos
            c = self.w2s(t.x, t.y)
            r = 8 + 34 * k
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 170, 40, int(230 * (1 - k))), 2.5))
            p.drawEllipse(c, r, r)

    # ------------------------------------------------------------------ brújula

    def _draw_compass_ring(self, p: QPainter, s: Snapshot):
        """Borde de la brújula, N/E/S/O siempre y, si el filtro lo pide, marcas cada 10° y rumbo."""
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        R = self._circle_r()
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(0, 0, 0, 170), 4))
        p.drawEllipse(c, R, R)
        p.setPen(QPen(QColor(143, 227, 161, 200), 1.5))
        p.drawEllipse(c, R - 1, R - 1)
        hdg = s.heading if (s.in_game and s.heading is not None) else 0.0
        if self.filters.helper("compass"):
            fsize = max(7, int(R / 14))
            p.setFont(QFont("Segoe UI", fsize, QFont.Bold))
            fm = p.fontMetrics()
            for deg in range(0, 360, 10):
                a = math.radians(deg - hdg)                 # 0 = arriba (rumbo arriba)
                ux, uy = math.sin(a), -math.cos(a)
                big = deg % 30 == 0
                L = R * (0.07 if big else 0.04)
                p.setPen(QPen(QColor(232, 239, 233, 210 if big else 140), 1.6 if big else 1))
                p.drawLine(QPointF(c.x() + ux * (R - 2), c.y() + uy * (R - 2)),
                           QPointF(c.x() + ux * (R - 2 - L), c.y() + uy * (R - 2 - L)))
            # rumbo actual arriba
            if s.in_game and s.heading is not None:
                txt = f"{s.heading:03.0f}°"
                w = fm.horizontalAdvance(txt) + 10
                box = QRectF(c.x() - w / 2, c.y() - R + R * 0.06, w, fm.height() + 2)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(0, 0, 0, 150))
                p.drawRoundedRect(box, 3, 3)
                p.setPen(QColor("#8fe3a1"))
                p.drawText(box, Qt.AlignCenter, txt)
        # puntos cardinales: siempre visibles, aunque el filtro de marcas esté apagado
        fsize = max(9, int(R / 11))
        font = QFont("Segoe UI", fsize, QFont.Bold)
        for deg, txt in ((0, "N"), (90, "E"), (180, "S"), (270, "O")):
            a = math.radians(deg - hdg)
            q = QPointF(c.x() + math.sin(a) * R * 0.76, c.y() - math.cos(a) * R * 0.76)
            path = QPainterPath()
            path.addText(0, 0, font, txt)
            br = path.boundingRect()
            path.translate(q.x() - br.center().x(), q.y() - br.center().y())
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 220), max(2.5, fsize / 4), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#ff5a48") if deg == 0 else QColor("#f2f6f3"))
            p.drawPath(path)
        # radio de la escala, abajo
        p.setFont(QFont("Segoe UI", max(7, int(R / 16))))
        fm = p.fontMetrics()
        txt = f"R {fmt_dist(self.radius_m)}"
        w = fm.horizontalAdvance(txt) + 10
        box = QRectF(c.x() - w / 2, c.y() + R - R * 0.06 - fm.height() - 2, w, fm.height() + 2)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QColor("#cfd8d2"))
        p.drawText(box, Qt.AlignCenter, txt)
        p.restore()

    def _rim_point(self, sp: QPointF):
        """Si un punto de pantalla cae fuera del círculo, su proyección en el borde; si no, None."""
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        R = self._circle_r()
        dx, dy = sp.x() - c.x(), sp.y() - c.y()
        d = math.hypot(dx, dy)
        if d <= R - 2 or d == 0:
            return None
        return QPointF(c.x() + dx / d * (R - 8), c.y() + dy / d * (R - 8)), (dx / d, dy / d)

    def _draw_rim_markers(self, p: QPainter, s: Snapshot):
        """Lo que queda fuera del radio se fija al borde, apuntando a su dirección real.

        Solo las categorías con "borde" activado en los filtros. Blanco fijado, base, punto
        marcado y aviones llevan su distancia. Para no amontonar texto, las etiquetas se ponen
        por prioridad (blanco > base > punto > aviones del más cercano al más lejano) y se
        omite la que choque con otra o con las letras/rumbo del anillo; la marca siempre sale.
        """
        F = self.filters
        rot = self._rot_transform()
        R = self._circle_r()
        c = QPointF(self.width() / 2.0, self.height() / 2.0)
        size = max(4.0, R * 0.035)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setFont(QFont("Segoe UI", max(6, int(R / 18)), QFont.Bold))
        fm = p.fontMetrics()
        items = []          # (prioridad, punto, dirección, color, forma, etiqueta)

        sx, sy = s.map_size
        for o in self.objs:
            if o.get("type") == "airfield" or o.get("icon") == "Player" or "x" not in o:
                continue
            side, cat, tgt = self._obj_side_cat(o)
            if not (F.object_on_rim(side, cat) or (tgt and F.special_rim("target"))):
                continue
            hit = self._rim_point(rot.map(self.n2s(o["x"], o["y"])))
            if not hit:
                continue
            d = math.hypot(o["x"] * sx - s.px, o["y"] * sy - s.py)
            if tgt and F.special_rim("target"):
                items.append((0, *hit, QColor("#ff5a4a"), "ring", fmt_dist(d)))
            elif o.get("type") == "aircraft":
                items.append((3 + d / 1e6, *hit, QColor(o.get("color", "#ccc")), "tri", fmt_dist(d)))
            else:
                items.append((9, *hit, QColor(o.get("color", "#ccc")), "dot", None))
        for af in s.airfields:
            side, cat = classify_airfield(af)
            if s.home is not None and af.key == s.home.key and F.special_rim("home"):
                continue                               # la base va aparte, con etiqueta
            if F.object_on_rim(side, cat):
                hit = self._rim_point(rot.map(self.w2s(af.cx, af.cy)))
                if hit:
                    items.append((9, *hit, QColor(af.color), "dot", None))
        if s.home and F.special_rim("home"):
            hit = self._rim_point(rot.map(self.w2s(s.home.cx, s.home.cy)))
            if hit:
                items.append((1, *hit, QColor("#7CFC9A"), "diamond", f"BASE {fmt_dist(s.home_dist)}"))
        if s.wp and F.special_rim("waypoint"):
            hit = self._rim_point(rot.map(self.w2s(*s.wp)))
            if hit:
                items.append((2, *hit, QColor("#FFD24A"), "diamond", fmt_dist(s.wp_dist)))

        # Zonas ocupadas por el anillo: rumbo (arriba), radio (abajo) y letras N/E/S/O
        taken = []
        bw, bh = R * 0.5, fm.height() + 4
        taken.append(QRectF(c.x() - bw / 2, c.y() - R, bw, R * 0.06 + bh + 2))
        taken.append(QRectF(c.x() - bw / 2, c.y() + R - R * 0.06 - bh - 2, bw, R * 0.06 + bh + 2))
        hdg = s.heading if s.heading is not None else 0.0
        half = max(9, int(R / 11)) * 0.9
        for deg in (0, 90, 180, 270):
            a = math.radians(deg - hdg)
            q = QPointF(c.x() + math.sin(a) * R * 0.76, c.y() - math.cos(a) * R * 0.76)
            taken.append(QRectF(q.x() - half, q.y() - half, 2 * half, 2 * half))

        # marcas (las de menor prioridad debajo)
        for pri, q, (ux, uy), col, shape, _ in sorted(items, key=lambda it: -it[0]):
            p.setPen(QPen(QColor(0, 0, 0, 200), 1.2))
            p.setBrush(col)
            if shape == "tri":                         # triángulo apuntando hacia fuera
                nx, ny = -uy, ux
                tip = QPointF(q.x() + ux * size * 1.3, q.y() + uy * size * 1.3)
                a = QPointF(q.x() - ux * size * 0.7 + nx * size, q.y() - uy * size * 0.7 + ny * size)
                b = QPointF(q.x() - ux * size * 0.7 - nx * size, q.y() - uy * size * 0.7 - ny * size)
                p.drawPolygon(QPolygonF([tip, a, b]))
            elif shape == "diamond":
                p.drawPolygon(QPolygonF([q + QPointF(0, -size * 1.2), q + QPointF(size * 1.2, 0),
                                         q + QPointF(0, size * 1.2), q + QPointF(-size * 1.2, 0)]))
            elif shape == "ring":
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(col, 2.2))
                p.drawEllipse(q, size * 1.5, size * 1.5)
            else:
                p.drawEllipse(q, size * 0.85, size * 0.85)

        # etiquetas por prioridad, sin solaparse
        for pri, q, (ux, uy), col, shape, label in sorted(items, key=lambda it: it[0]):
            if not label:
                continue
            w = fm.horizontalAdvance(label)
            lc = QPointF(q.x() - ux * (size * 2 + w * 0.5 + 4), q.y() - uy * (size * 2 + fm.height() * 0.5))
            r = QRectF(lc.x() - w / 2 - 3, lc.y() - fm.height() / 2, w + 6, fm.height())
            if any(r.intersects(t) for t in taken):
                continue
            taken.append(r)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            p.drawRoundedRect(r, 3, 3)
            p.setPen(col.lighter(130))
            p.drawText(r, Qt.AlignCenter, label)
        p.restore()
