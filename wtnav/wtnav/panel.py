"""Panel lateral con los cálculos que el juego no muestra.

Secciones reordenables (grip ⠿) y ocultables (×).
El contenido se escala automáticamente para que todos los bloques visibles
quepan en la ventana sin recortarse, independientemente del tamaño.
En modo paisaje (ancho > alto) las secciones se distribuyen en columnas.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QCoreApplication, QEvent, Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFontInfo, QTransform
from PySide6.QtWidgets import (QFrame, QGraphicsScene, QGraphicsView,
                                QGridLayout, QHBoxLayout, QLabel,
                                QMenu, QPushButton, QToolButton,
                                QVBoxLayout, QWidget)

from .calc import Snapshot, fmt_dist, fmt_time

OK, WARN, CRIT, DIM, ACC = "#8fe3a1", "#ffcf5a", "#ff6b5a", "#7d8a83", "#e8efe9"
_BG = "#141816"

# Tamaño mínimo legible del texto del panel. El panel encoge para que quepa todo, pero nunca
# por debajo de esto; a partir de ahí aparece barra de desplazamiento.
# Se cumplen las dos condiciones a la vez:
#   - MIN_TEXT_MM: tamaño físico en pantalla (mm). Depende de la densidad de píxeles del
#     monitor y del escalado de Windows: un portátil 1080p de 15" necesita más escala que
#     un monitor 1080p de 24", porque sus píxeles son más pequeños.
#   - MIN_TEXT_PX: píxeles reales por letra. Por debajo, el texto se emborrona aunque
#     físicamente sea grande (monitores de baja densidad).
MIN_TEXT_MM = 2.3
MIN_TEXT_PX = 10

SECTION_DEFS: dict[str, tuple[str, list]] = {
    "nav":    ("Vuelta a base",    [("base","Base"),("dist","Distancia"),("brg","Rumbo"),
                                    ("hdg","Rumbo corregido"),("eta","Llegada"),
                                    ("gs","Vel. suelo"),("wind","Viento est.")]),
    "app":    ("Aproximación",     [("rwy","Pista"),("iaf","Punto entrada (8 km)"),
                                    ("along","Al umbral"),("lat","Eje"),
                                    ("gp","Senda 3°"),("elev","Elev. pista")]),
    "fuel":   ("Combustible",      [("burn","Consumo"),("endu","Autonomía"),("range","Alcance"),
                                    ("home","Para volver"),("margin","Margen bingo")]),
    "energy": ("Energía y giro",   [("ps","Ps (energía)"),("es","Altura energética"),
                                    ("turn","Régimen de giro"),("radius","Radio de giro"),
                                    ("t360","Tiempo 360°"),("glide","Planeo")]),
    "engine": ("Motor",            [("wep","WEP actual"),("wept","WEP total"),
                                    ("e1","Motor 1"),("e2","Motor 2"),
                                    ("e3","Motor 3"),("e4","Motor 4")]),
    "wp":     ("Punto marcado (clic en mapa)", [("dist","Distancia"),("brg","Rumbo"),
                                               ("hdg","Rumbo corregido"),("eta","Llegada"),
                                               ("bomb","Suelta bomba (aprox.)"),
                                               ("btime","Tiempo a suelta")]),
    "tgt":    ("Objetivos cercanos", [(f"t{i}","") for i in range(6)] + [("threat","Enemigo aéreo"),
                                                                    ("aa","Antiaéreo enemigo")]),
    "sess":   ("Sesión",           [("time","Tiempo de vuelo"),("dist","Recorrido"),("g","G máx.")]),
}
DEFAULT_ORDER = ["nav", "app", "fuel", "energy", "engine", "wp", "tgt", "sess"]


def turn_txt(t):
    if t is None:
        return ""
    if abs(t) < 3:
        return "de frente"
    return f"gira {abs(t):.0f}° {'dcha' if t > 0 else 'izq'}"


# ---------------------------------------------------------------------------
# Grip de arrastre
# ---------------------------------------------------------------------------

class DragGrip(QLabel):
    drag_started = Signal(QPoint)
    drag_moved   = Signal(QPoint)
    drag_ended   = Signal()

    def __init__(self):
        super().__init__("⠿")
        self.setFixedWidth(16)
        self._active = False
        self._enabled = True
        self._refresh_style()

    def set_drag_enabled(self, on: bool):
        self._enabled = on
        self.setCursor(Qt.SizeVerCursor if on else Qt.ArrowCursor)
        self.setToolTip("Arrastra para reordenar" if on else "")
        self._refresh_style()

    def _refresh_style(self):
        color = "#8fe3a1" if self._active else ("#3a4a40" if self._enabled else "#1f2a24")
        self.setStyleSheet(f"color:{color}; font-size:12pt; background:transparent;")

    def mousePressEvent(self, e):
        if self._enabled and e.button() == Qt.LeftButton:
            self._active = True
            self.grabMouse()
            self._refresh_style()
            self.drag_started.emit(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._active:
            self.drag_moved.emit(e.globalPosition().toPoint())

    def mouseReleaseEvent(self, e):
        if self._active and e.button() == Qt.LeftButton:
            self._active = False
            self.releaseMouse()
            self._refresh_style()
            self.drag_ended.emit()


# ---------------------------------------------------------------------------
# Sección
# ---------------------------------------------------------------------------

class Section(QFrame):
    hide_requested = Signal(str)

    def __init__(self, key: str, title: str, rows: list):
        super().__init__()
        self._key = key
        self.setObjectName("section")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── cabecera ─────────────────────────────────────────────────────
        header = QFrame()
        header.setFixedHeight(26)
        header.setStyleSheet(
            "QFrame { background:transparent; border:none;"
            " border-bottom:1px solid #252e28; }"
        )
        hlay = QHBoxLayout(header)
        hlay.setContentsMargins(6, 0, 6, 0)
        hlay.setSpacing(4)

        self.grip = DragGrip()

        title_lbl = QLabel(title.upper())
        title_lbl.setObjectName("sectionTitle")

        hide_btn = QToolButton()
        hide_btn.setText("×")
        hide_btn.setFixedSize(16, 16)
        hide_btn.setToolTip("Ocultar sección")
        hide_btn.setStyleSheet(
            "QToolButton { color:#3d4d44; border:none; background:transparent; font-size:11pt; }"
            "QToolButton:hover { color:#ff6b5a; }"
        )
        hide_btn.clicked.connect(lambda: self.hide_requested.emit(key))

        hlay.addWidget(self.grip)
        hlay.addWidget(title_lbl, 1)
        hlay.addWidget(hide_btn)
        outer.addWidget(header)

        # ── contenido ────────────────────────────────────────────────────
        content = QWidget()
        content.setStyleSheet("background:transparent;")
        grid = QGridLayout(content)
        grid.setContentsMargins(10, 6, 10, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)

        self.vals: dict[str, QLabel] = {}
        self.keys: dict[str, QLabel] = {}
        self._last: dict[str, str] = {}
        for i, (row_key, label) in enumerate(rows):
            k = QLabel(label)
            k.setObjectName("key")
            v = QLabel("--")
            v.setObjectName("val")
            v.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            v.setTextFormat(Qt.RichText)
            grid.addWidget(k, i, 0)
            grid.addWidget(v, i, 1)
            self.vals[row_key] = v
            self.keys[row_key] = k
        grid.setColumnStretch(1, 1)
        outer.addWidget(content)

    def set(self, key, text, color=ACC):
        # setText re-maqueta y repinta la etiqueta aunque el texto sea el mismo: solo si cambia
        html = f'<span style="color:{color}">{text}</span>'
        if self._last.get(key) != html:
            self._last[key] = html
            self.vals[key].setText(html)

    def show_row(self, key, visible):
        if self.vals[key].isVisibleTo(self) != visible:
            self.vals[key].setVisible(visible)
            self.keys[key].setVisible(visible)

    def set_drag_highlight(self, active: bool):
        if active:
            self.setStyleSheet(
                "QFrame#section { background:#1b201d; border:1px solid #8fe3a1;"
                " border-radius:6px; }"
            )
        else:
            self.setStyleSheet("")


# ---------------------------------------------------------------------------
# Panel  (QWidget con QGraphicsView para escalar el contenido)
# ---------------------------------------------------------------------------

class InfoPanel(QWidget):
    """
    El contenido se renderiza a su tamaño natural y se escala uniformemente
    para que todo quepa en la ventana. Cuando el ancho supera al alto se
    organiza en varias columnas para aprovechar el espacio horizontal.
    """

    def __init__(self, settings=None):
        super().__init__()
        self._settings = settings
        self._sections: dict[str, Section] = {}
        self._order: list[str] = list(DEFAULT_ORDER)
        self._hidden: set[str] = set()
        self._drag_key: str | None = None
        self._n_cols: int = 1
        self._content_h: int = -1

        self._load_state()
        self._build_ui()
        # Ajuste inicial diferido (el widget aún no tiene tamaño definitivo)
        QTimer.singleShot(0, self._fit_to_view)

    # ── persistencia ─────────────────────────────────────────────────────

    def _load_state(self):
        if not self._settings:
            return
        # type=list: en Windows QSettings puede devolver str o '' en vez de lista
        saved_order = self._settings.value("panel_order", [], type=list)
        if isinstance(saved_order, list):
            valid = [k for k in saved_order if k in SECTION_DEFS]
            for k in DEFAULT_ORDER:
                if k not in valid:
                    valid.append(k)
            self._order = valid
        saved_hidden = self._settings.value("panel_hidden", [], type=list)
        if isinstance(saved_hidden, list):
            self._hidden = set(saved_hidden) & set(SECTION_DEFS)

    def _save_state(self):
        if self._settings:
            self._settings.setValue("panel_order", self._order)
            self._settings.setValue("panel_hidden", list(self._hidden))

    # ── construcción ─────────────────────────────────────────────────────

    def _build_ui(self):
        # Vista escalable
        self._scene = QGraphicsScene(self)
        self._view = QGraphicsView(self._scene, self)
        self._view.setFrameShape(QFrame.NoFrame)
        self._view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._view.setStyleSheet(
            "QScrollBar:vertical { background:#141816; width:8px; margin:0; }"
            "QScrollBar::handle:vertical { background:#2f3a33; border-radius:4px; min-height:30px; }"
            "QScrollBar::handle:vertical:hover { background:#8fe3a1; }"
            "QScrollBar::add-line, QScrollBar::sub-line { height:0; }"
            "QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }")
        self._view.setBackgroundBrush(QBrush(QColor(_BG)))
        self._view.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._view.setInteractive(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._view)

        # Contenido real (renderizado en la escena)
        self._body = QWidget()
        self._body.setStyleSheet(f"background:{_BG};")
        body_lay = QVBoxLayout(self._body)
        body_lay.setContentsMargins(6, 6, 6, 6)
        body_lay.setSpacing(6)
        body_lay.setAlignment(Qt.AlignTop)   # sin stretch: altura natural

        self.alerts = QLabel()
        self.alerts.setObjectName("alerts")
        self.alerts.setWordWrap(True)
        self.alerts.setTextFormat(Qt.RichText)
        body_lay.addWidget(self.alerts)

        self._sec_container = QWidget()
        self._sec_container.setStyleSheet("background:transparent;")
        self._col_layout = QHBoxLayout(self._sec_container)
        self._col_layout.setContentsMargins(0, 0, 0, 0)
        self._col_layout.setSpacing(6)
        body_lay.addWidget(self._sec_container)

        self._restore_btn = QPushButton()
        self._restore_btn.setStyleSheet(
            "QPushButton { background:transparent; border:1px dashed #333c36;"
            " border-radius:4px; color:#7d8a83; padding:5px; font-size:9pt; }"
            "QPushButton:hover { color:#8fe3a1; border-color:#8fe3a1; }"
        )
        self._restore_btn.clicked.connect(self._show_restore_menu)
        body_lay.addWidget(self._restore_btn)

        # Añadir el cuerpo a la escena
        self._proxy = self._scene.addWidget(self._body)

        # Crear secciones
        for key in DEFAULT_ORDER:
            title, rows = SECTION_DEFS[key]
            sec = Section(key, title, rows)
            sec.hide_requested.connect(self._hide_section)
            sec.grip.drag_started.connect(lambda pos, k=key: self._on_drag_start(k, pos))
            sec.grip.drag_moved.connect(self._on_drag_move)
            sec.grip.drag_ended.connect(self._on_drag_end)
            self._sections[key] = sec

        self._rebuild_layout()
        self.setMinimumSize(200, 150)

    # ── escalado y columnas ───────────────────────────────────────────────

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._view.setGeometry(self.rect())
        self._update_layout_and_scale()

    def _update_layout_and_scale(self):
        # Tamaño de la vista completa (no del viewport): la barra de desplazamiento no debe
        # cambiar el número de columnas, o podría entrar en bucle cerca del umbral.
        vw, vh = self._view.width(), self._view.height()
        if vw <= 0 or vh <= 0:
            return

        # Columnas según proporción del panel
        n = 1 if vh >= vw else max(1, min(vw // 280, 4))
        if n != self._n_cols:
            self._n_cols = n
            self._rebuild_layout()   # llama a _fit_to_view al final
        else:
            self._fit_to_view()

    def _min_scale(self) -> float:
        """Escala mínima para que el texto siga siendo legible en el monitor donde está el panel."""
        ref = next(iter(self._sections.values())).keys
        lbl = next(iter(ref.values()), None)
        base_px = QFontInfo(lbl.font()).pixelSize() if lbl else 13     # px lógicos del texto normal
        if base_px <= 0:
            return 0.75
        dpr = self.devicePixelRatioF()                                 # escalado de Windows (1.25 = 125 %)
        need = MIN_TEXT_PX / (base_px * dpr)
        scr = self.screen()
        if scr:
            dpi = scr.physicalDotsPerInch()      # px lógicos por pulgada física del monitor
            if 50 <= dpi <= 400:                  # algunos monitores reportan valores absurdos por EDID
                need = max(need, (MIN_TEXT_MM / 25.4 * dpi) / base_px)
        return max(0.3, min(1.0, need))

    def _fit_to_view(self):
        """Escala el contenido para que quepa en la vista, sin bajar del mínimo legible.

        El cuerpo se maqueta a ancho = ancho_vista / escala, para que una vez escalado ocupe
        todo el ancho (si no, al encoger quedaba una franja vacía a la derecha).
        """
        if not hasattr(self, '_view') or not hasattr(self, '_body'):
            return
        vp = self._view.viewport()
        if not vp:
            return
        vh = vp.height()
        vw_full = self._view.width()
        if vw_full <= 0 or vh <= 0:
            return
        min_s = self._min_scale()
        sb_w = self._view.verticalScrollBar().sizeHint().width()

        scale, scroll, content_h = 1.0, False, 0
        for _ in range(4):                 # converge en 1-2 pasadas (el ancho apenas cambia la altura)
            vw = vw_full - (sb_w if scroll else 0)
            self._body.setFixedWidth(max(1, int(vw / scale)))
            self._activate_layouts()
            content_h = self._body.sizeHint().height()
            if content_h <= 0:
                return
            new_scale = max(min_s, min(1.0, vh / content_h))
            new_scroll = content_h * new_scale > vh + 1
            if abs(new_scale - scale) < 0.002 and new_scroll == scroll:
                break
            scale, scroll = new_scale, new_scroll
        self._content_h = content_h
        self._scale = scale

        self._view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn if scroll else Qt.ScrollBarAlwaysOff)
        t = QTransform()
        t.scale(scale, scale)
        self._view.setTransform(t)
        self._scene.setSceneRect(0, 0, self._body.width(), content_h)

    # ── cambio de monitor ─────────────────────────────────────────────────

    def showEvent(self, e):
        super().showEvent(e)
        # Al separar el panel en su ventana o moverlo a otro monitor cambia la pantalla:
        # el mínimo legible depende de ella, así que se recalcula.
        wh = self.window().windowHandle()
        if wh is not None and wh is not getattr(self, "_screen_watch", None):
            self._screen_watch = wh
            wh.screenChanged.connect(lambda _scr: QTimer.singleShot(0, self._fit_to_view))
        QTimer.singleShot(0, self._fit_to_view)

    # ── layout multi-columna ──────────────────────────────────────────────

    def _rebuild_layout(self):
        lay = self._col_layout

        # Rescatar secciones antes de destruir columnas antiguas
        for sec in self._sections.values():
            sec.setParent(self._sec_container)
            sec.hide()

        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.setParent(None)

        visible = [k for k in self._order if k not in self._hidden]
        hidden  = [k for k in self._order if k in self._hidden]
        n = self._n_cols
        per_col = math.ceil(len(visible) / n) if visible else 1
        drag_ok = (n == 1)

        col_lays: list[QVBoxLayout] = []
        for _ in range(n):
            cw = QWidget(self._sec_container)
            cw.setStyleSheet("background:transparent;")
            cl = QVBoxLayout(cw)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(6)
            cl.setAlignment(Qt.AlignTop)
            col_lays.append(cl)
            lay.addWidget(cw)

        for i, key in enumerate(visible):
            ci = min(i // per_col, n - 1)
            col_lays[ci].addWidget(self._sections[key])
            self._sections[key].show()
            self._sections[key].grip.set_drag_enabled(drag_ok)

        for key in hidden:
            col_lays[0].addWidget(self._sections[key])

        self._update_restore_btn()
        self._fit_to_view()

    # ── ocultar / restaurar ───────────────────────────────────────────────

    def _hide_section(self, key: str):
        self._hidden.add(key)
        self._sections[key].setVisible(False)
        self._update_restore_btn()
        self._fit_to_view()
        self._save_state()

    def _restore_section(self, key: str):
        self._hidden.discard(key)
        self._rebuild_layout()
        self._save_state()

    def _restore_all(self):
        self._hidden.clear()
        self._rebuild_layout()
        self._save_state()

    def _update_restore_btn(self):
        hidden_in_order = [k for k in self._order if k in self._hidden]
        if hidden_in_order:
            count = len(hidden_in_order)
            label = "sección oculta" if count == 1 else f"{count} secciones ocultas"
            self._restore_btn.setText(f"⊕  {label} — clic para restaurar")
            self._restore_btn.setVisible(True)
        else:
            self._restore_btn.setVisible(False)

    def _show_restore_menu(self):
        hidden_in_order = [k for k in self._order if k in self._hidden]
        if not hidden_in_order:
            return
        menu = QMenu(self)
        for key in hidden_in_order:
            title = SECTION_DEFS[key][0]
            action = menu.addAction(f"Mostrar: {title}")
            action.triggered.connect(lambda checked=False, k=key: self._restore_section(k))
        if len(hidden_in_order) > 1:
            menu.addSeparator()
            menu.addAction("Mostrar todas").triggered.connect(self._restore_all)
        menu.exec(self._restore_btn.mapToGlobal(QPoint(0, 0)))

    # ── drag & drop (solo en modo una columna) ────────────────────────────

    def _on_drag_start(self, key: str, _pos: QPoint):
        self._drag_key = key
        self._sections[key].set_drag_highlight(True)

    def _on_drag_move(self, global_pos: QPoint):
        if not self._drag_key:
            return
        visible = [k for k in self._order if k not in self._hidden]
        if self._drag_key not in visible:
            return
        drag_vi = visible.index(self._drag_key)

        local_y = self._sec_container.mapFromGlobal(global_pos).y()

        new_vi = len(visible) - 1
        for i, key in enumerate(visible):
            sec = self._sections[key]
            mid_global = sec.mapToGlobal(QPoint(0, sec.height() // 2))
            mid_y = self._sec_container.mapFromGlobal(mid_global).y()
            if local_y < mid_y:
                new_vi = i
                break

        if new_vi == drag_vi:
            return

        visible.insert(new_vi, visible.pop(drag_vi))
        hidden = [k for k in self._order if k in self._hidden]
        self._order = visible + hidden
        self._rebuild_layout()

    def _on_drag_end(self):
        if self._drag_key:
            self._sections[self._drag_key].set_drag_highlight(False)
            self._drag_key = None
        self._save_state()

    # ── refresco de datos ─────────────────────────────────────────────────

    def refresh(self, s: Snapshot):
        self._alerts(s)
        in_game_changed = False
        for key, sec in self._sections.items():
            should_be_visible = s.in_game and key not in self._hidden
            if sec.isVisibleTo(self) != should_be_visible:
                sec.setVisible(should_be_visible)
                in_game_changed = True
        if in_game_changed:
            self._fit_to_view()
        if not s.in_game:
            return
        self._nav(s)
        self._app(s)
        self._fuel(s)
        self._energy(s)
        self._engine(s)
        self._wp(s)
        self._targets(s)
        sess = self._sections["sess"]
        sess.set("time", fmt_time(s.flight_time))
        sess.set("dist", fmt_dist(s.flown_m))
        sess.set("g", f"{s.max_g:.1f} G")
        # Filas de objetivos/motores aparecen y desaparecen: si cambia la altura, reescalar.
        # Diferido para que el layout haya recalculado ya su tamaño.
        QTimer.singleShot(0, self._refit_if_needed)

    def _activate_layouts(self):
        # Al ocultar/mostrar filas, Qt deja el reajuste como evento pendiente (LayoutRequest)
        # y hasta procesarlo sizeHint devuelve la altura vieja. Se procesan antes de medir.
        # Cada nivel de anidamiento (cuadrícula -> sección -> columna -> contenedor -> cuerpo)
        # genera el evento del nivel superior al procesar el suyo, y la altura puede no cambiar
        # en los pasos intermedios: se hacen pasadas suficientes para cubrir todos los niveles.
        for _ in range(8):
            QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)

    def _refit_if_needed(self):
        self._activate_layouts()
        h = self._body.sizeHint().height()
        if h != self._content_h:
            self._fit_to_view()

    def _alerts(self, s):
        was_visible = self.alerts.isVisible()
        if not s.alerts:
            self.alerts.hide()
            if was_visible:
                self._fit_to_view()
            return
        html = "<br>".join(
            f'<span style="color:{CRIT if lvl == "crit" else WARN};'
            f' font-weight:bold">&#9650; {t}</span>'
            for lvl, t in s.alerts)
        self.alerts.setText(html)
        self.alerts.show()
        if not was_visible:
            self._fit_to_view()

    def _nav(self, s):
        n = self._sections["nav"]
        if s.home is None:
            for k in n.vals: n.set(k, "--", DIM)
            n.set("base", "sin aeródromos", DIM)
            return
        n.set("base", ("auto" if s.home_auto else "elegida") + " · doble clic para cambiar", DIM)
        n.set("dist", fmt_dist(s.home_dist))
        n.set("brg", f"{s.home_brg:03.0f}°  <span style='color:{DIM}'>{turn_txt(s.home_turn)}</span>")
        n.set("hdg", f"{s.home_hdg_corr:03.0f}°" if s.home_hdg_corr is not None else "--")
        n.set("eta", fmt_time(s.home_eta))
        n.set("gs", f"{s.gs * 3.6:.0f} km/h" if s.gs else "--")
        if s.wind_speed is not None:
            n.set("wind", f"{s.wind_speed * 3.6:.0f} km/h desde {s.wind_from:03.0f}°"
                  if s.wind_speed >= 1.5 else "en calma")
        else:
            n.set("wind", "midiendo (vuela recto)", DIM)

    def _app(self, s):
        a = self._sections["app"]
        if s.rwy_hdg is None:
            for k in a.vals: a.set(k, "--", DIM)
            return
        a.set("rwy", f"{s.rwy_hdg:03.0f}° · {fmt_dist(s.home.length)}")
        a.set("iaf", f"{fmt_dist(s.entry_dist)} · {s.entry_brg:03.0f}°")
        if s.app_along is not None and s.app_along > 0:
            a.set("along", fmt_dist(s.app_along))
            lat = s.app_lateral or 0.0
            col = OK if abs(lat) < 100 else WARN if abs(lat) < 400 else CRIT
            side = "a la dcha → corrige izq" if lat > 0 else "a la izq → corrige dcha"
            a.set("lat", "centrado" if abs(lat) < 30 else f"{fmt_dist(abs(lat))} {side}", col)
            if s.app_dev is not None and s.app_along < 15000:
                d = s.app_dev
                col = OK if abs(d) < 40 else WARN if abs(d) < 120 else CRIT
                txt = "en senda" if abs(d) < 15 else f"{abs(d):.0f} m {'alto' if d > 0 else 'bajo'}"
                a.set("gp", f"{txt} <span style='color:{DIM}'>(obj. {s.app_target_agl:.0f} m)</span>", col)
            else:
                a.set("gp", "lejos", DIM)
        else:
            a.set("along", "pasado el umbral", DIM)
            a.set("lat", "--", DIM)
            a.set("gp", "--", DIM)
        a.set("elev",
              f"{s.field_elev:.0f} m" if s.field_elev_known else "desconocida (supongo 0 m)",
              ACC if s.field_elev_known else DIM)

    def _fuel(self, s):
        f = self._sections["fuel"]
        if s.fuel is None:
            for k in f.vals: f.set(k, "--", DIM)
            return
        if s.burn is None:
            for k in f.vals: f.set(k, "midiendo...", DIM)
            return
        f.set("burn", f"{s.burn * 60:.1f} kg/min")
        f.set("endu", fmt_time(s.endurance))
        f.set("range", fmt_dist(s.range_m))
        f.set("home",
              f"{s.fuel_home:.0f} kg + {s.reserve_kg:.0f} reserva"
              if s.fuel_home is not None else "--")
        col = {"OK": OK, "JUSTO": WARN, "BINGO": CRIT}.get(s.fuel_status, ACC)
        f.set("margin",
              f"{fmt_time(s.bingo_margin_s)} · {s.fuel_status}"
              if s.bingo_margin_s is not None else "--", col)

    def _energy(self, s):
        e = self._sections["energy"]
        if s.ps is not None:
            col = OK if s.ps > 2 else CRIT if s.ps < -5 else WARN if s.ps < -1 else ACC
            word = "ganando" if s.ps > 1 else "perdiendo" if s.ps < -1 else "sostenido"
            e.set("ps", f"{s.ps:+.1f} m/s · {word}", col)
        else:
            e.set("ps", "--", DIM)
        e.set("es", fmt_dist(s.es) if s.es is not None else "--")
        tr = s.turn_rate
        e.set("turn", f"{abs(tr):.1f} °/s" if tr is not None and abs(tr) > 1 else "recto", ACC)
        e.set("radius", fmt_dist(s.turn_radius) if s.turn_radius else "--")
        e.set("t360", f"{s.turn_360:.0f} s" if s.turn_360 and s.turn_360 < 120 else "--")
        if s.glide_ratio:
            ok = ("" if s.glide_home_ok is None
                  else " · llegas a base" if s.glide_home_ok else " · NO llegas")
            col = ((OK if s.glide_home_ok else CRIT)
                   if s.glide_live and s.glide_home_ok is not None else ACC)
            at = f" a {s.glide_speed * 3.6:.0f} km/h" if s.glide_speed else ""
            e.set("glide", f"1:{s.glide_ratio:.0f}{at} → {fmt_dist(s.glide_reach)}{ok}", col)
        else:
            e.set("glide", "corta motor para medir", DIM)

    def _engine(self, s):
        m = self._sections["engine"]
        # En reactores el 110 % del acelerador es la postcombustión, no el WEP
        m.keys["wep"].setText("Postcombustión" if s.is_jet else "WEP actual")
        m.keys["wept"].setText("Postcomb. total" if s.is_jet else "WEP total")
        m.set("wep", fmt_time(s.wep_now) if s.wep_now > 0 else "off",
              WARN if s.wep_now > 0 else DIM)
        m.set("wept", fmt_time(s.wep_total))
        for i in range(1, 5):
            key = f"e{i}"
            eng = next((x for x in s.engines if x.idx == i), None)
            m.show_row(key, eng is not None)
            if not eng:
                continue
            parts = []
            for name, val, rate, ttl in (("agua",   eng.water, eng.water_rate, eng.water_ttl),
                                         ("aceite", eng.oil,   eng.oil_rate,   eng.oil_ttl)):
                if val is None:
                    continue
                if rate is None:
                    parts.append(f"{name} ...")
                    continue
                arrow = "&#8593;" if rate > 0.3 else "&#8595;" if rate < -0.3 else "="
                col = ACC
                extra = ""
                if ttl is not None:
                    col = CRIT if ttl < 15 else WARN if ttl < 60 else ACC
                    extra = f" lím. {fmt_time(ttl)}"
                parts.append(
                    f"<span style='color:{col}'>{name} {arrow}{abs(rate):.1f}°/min{extra}</span>"
                )
            m.set(key, "<br>".join(parts) if parts else "--")

    def _wp(self, s):
        w = self._sections["wp"]
        if s.wp is None:
            for k in w.vals: w.set(k, "--", DIM)
            w.set("dist", "clic izq. marca · dcho. borra", DIM)
            return
        w.set("dist", fmt_dist(s.wp_dist))
        w.set("brg", f"{s.wp_brg:03.0f}°  <span style='color:{DIM}'>{turn_txt(s.wp_turn)}</span>")
        w.set("hdg", f"{s.wp_hdg_corr:03.0f}°" if s.wp_hdg_corr is not None else "--")
        w.set("eta", fmt_time(s.wp_eta))
        if s.bomb_release:
            w.set("bomb", f"{fmt_dist(s.bomb_release)} antes · caída {s.bomb_fall:.1f} s")
            bt = s.bomb_time
            col = CRIT if bt is not None and 0 <= bt < 5 else ACC
            w.set("btime", fmt_time(bt) if bt is not None and bt >= 0 else "pasado", col)
        else:
            w.set("bomb", "--", DIM)
            w.set("btime", "--", DIM)

    def _targets(self, s):
        names = {"bombing_point": "Pto. bombardeo", "capture_zone": "Zona captura",
                 "defending_point": "Pto. defensa", "waypoint": "Waypoint",
                 "Airdefence": "Antiaéreo", "Structure": "Estructura",
                 "Frigate": "Fragata", "Destroyer": "Destructor", "MissileDestroyer": "Destructor misiles",
                 "Boat": "Lancha", "Cruiser": "Crucero", "Battleship": "Acorazado"}
        t = self._sections["tgt"]
        for i in range(6):
            key = f"t{i}"
            if i < len(s.targets):
                tg = s.targets[i]
                col = CRIT if tg.side == "enemy" else OK if tg.side == "friendly" else ACC
                t.keys[key].setText(names.get(tg.name, tg.name))
                t.set(key, f"{fmt_dist(tg.dist)} · {tg.brg:03.0f}° · {fmt_time(tg.eta)}", col)
                t.show_row(key, True)
            else:
                t.show_row(key, False)
        if s.threat:
            t.set("threat", f"{fmt_dist(s.threat.dist)} · {s.threat.brg:03.0f}°", CRIT)
        else:
            t.set("threat", "ninguno visible", DIM)
        if s.aa_threat:
            t.set("aa", f"{s.aa_threat.name} · {fmt_dist(s.aa_threat.dist)} · {s.aa_threat.brg:03.0f}°", CRIT)
        else:
            t.set("aa", "ninguno visible", DIM)
