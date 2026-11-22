"""WT Nav: copiloto de navegación para War Thunder (Simulador aire)."""
from __future__ import annotations

import json
import sys
import time

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QIcon, QKeySequence, QPixmap, QPainter, QColor, QPolygonF
from PySide6.QtCore import QPoint, QPointF
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QSlider, QSplitter,
                               QToolBar, QToolButton, QWidgetAction, QCheckBox, QPushButton,
                               QVBoxLayout, QWidget, QSizePolicy)

from .api import WTClient
from .calc import FlightModel
from .mapview import MapWidget
from .panel import InfoPanel
from .titlebar import CaptionButtons, FramelessChrome, WindowDragger
from .filters import MapFilters
from .filters_ui import FilterPopup
from .circlewin import CircleMapWindow

APP_NAME = "WT Nav"
VERSION = "1.0"

STYLE = """
QMainWindow, QWidget { background: #141816; color: #e8efe9; font-family: 'Segoe UI'; font-size: 10pt; }
QLabel { background: transparent; }
QToolBar QWidget { background: transparent; }
QWidget#header { background: #1c211e; }
QToolBar { background: #1c211e; border: none; spacing: 6px; padding: 3px; }
QToolBar QToolButton { padding: 4px 8px; border-radius: 4px; }
QToolBar QToolButton:checked { background: #2f5a3d; }
QToolBar QToolButton:hover { background: #2a312d; }
QPushButton#floatOpt { background: rgba(34,41,37,220); color: #e8efe9; border: 1px solid #333c36;
                       border-radius: 4px; padding: 3px 10px; }
QPushButton#floatOpt:hover { background: #2f5a3d; }
QToolButton#settingsBtn { padding: 4px 10px; border-radius: 4px; background: transparent; }
QToolButton#settingsBtn:hover { background: #2a312d; }
QToolButton#settingsBtn::menu-indicator { image: none; width: 0; }
QToolButton#qt_toolbar_ext_button { background: #2a312d; border-radius: 3px; padding: 0; }
QToolButton#qt_toolbar_ext_button:hover { background: #2f5a3d; }
QLineEdit, QDoubleSpinBox { background: #222925; border: 1px solid #333c36; border-radius: 4px; padding: 2px 6px; }
QFrame#section { background: #1b201d; border: 1px solid #262e29; border-radius: 6px; }
QLabel#sectionTitle { color: #8fb79a; font-size: 8pt; font-weight: bold; letter-spacing: 1px; }
QLabel#key { color: #93a39a; }
QLabel#val { font-weight: 600; }
QLabel#alerts { background: #2b1714; border: 1px solid #6b2a22; border-radius: 6px; padding: 8px; }
QScrollArea { border: none; }
QStatusBar { background: #1c211e; color: #93a39a; }
QSplitter::handle { background: #262e29; width: 3px; }
QSlider::groove:horizontal { height: 4px; background: #333c36; border-radius: 2px; }
QSlider::handle:horizontal { background: #8fe3a1; width: 12px; margin: -5px 0; border-radius: 6px; }
QMenu { background: #1c211e; border: 1px solid #333c36; }
QMenu::item { padding: 4px 20px; }
QMenu::item:selected { background: #2f5a3d; }
"""


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1b201d"))
    p.setPen(QColor("#8fe3a1"))
    p.drawEllipse(2, 2, 60, 60)
    p.setBrush(QColor("#e8efe9"))
    p.drawPolygon(QPolygonF([QPointF(32, 10), QPointF(46, 50), QPointF(32, 42), QPointF(18, 50)]))
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------------------
# Diálogo de límites de temperatura
# ---------------------------------------------------------------------------

def _chevron_icon() -> QIcon:
    # 10 px de ancho: el botón » de Qt mide ~12 px y su anchura no se puede ampliar
    pm = QPixmap(10, 16)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    from PySide6.QtGui import QPen
    p.setPen(QPen(QColor("#e8efe9"), 1.5))
    for x in (1.0, 5.0):
        p.drawLine(QPointF(x, 4), QPointF(x + 3.5, 8))
        p.drawLine(QPointF(x + 3.5, 8), QPointF(x, 12))
    p.end()
    return QIcon(pm)


class LimitsDialog(QDialog):
    def __init__(self, aircraft: str, limits: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Límites de temperatura")
        lay = QFormLayout(self)
        lay.addRow(QLabel(f"<b>{aircraft or 'avión desconocido'}</b><br>"
                          "<span style='color:#93a39a'>Se guardan por avión. 0 = sin límite.<br>"
                          "Con límite, el panel calcula cuánto falta para alcanzarlo.</span>"))
        self.water = QDoubleSpinBox()
        self.oil = QDoubleSpinBox()
        for sb, key in ((self.water, "water"), (self.oil, "oil")):
            sb.setRange(0, 400)
            sb.setSuffix(" °C")
            sb.setValue(float(limits.get(key) or 0))
        lay.addRow("Agua / refrigerante", self.water)
        lay.addRow("Aceite", self.oil)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)

    def values(self):
        out = {}
        if self.water.value() > 0:
            out["water"] = self.water.value()
        if self.oil.value() > 0:
            out["oil"] = self.oil.value()
        return out


# ---------------------------------------------------------------------------
# Ventana flotante genérica
# ---------------------------------------------------------------------------

class FloatingWindow(QWidget):
    """Ventana independiente para el mapa o el panel de información.

    Sin marco de Windows: usa una barra de título propia, transparente, que solo aparece
    con el ratón encima (ver titlebar.py).
    """
    reattach_requested = Signal()

    def __init__(self, title: str, settings: QSettings, geo_key: str, icon: QIcon = None,
                 extra_buttons=()):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle(title)
        if icon:
            self.setWindowIcon(icon)
        self._settings = settings
        self._geo_key = geo_key
        self._suppress_signal = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self._lay = lay
        self.setMinimumSize(220, 180)
        self._chrome = FramelessChrome(self, title)

        # Opciones de esta ventana (aparecen abajo al pasar el ratón): opacidad propia y extras
        opts = QWidget()
        ol = QHBoxLayout(opts)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(8)
        for b in extra_buttons:
            b.setParent(opts)
            b.setObjectName("floatOpt")
            ol.addWidget(b)
        ol.addStretch(1)
        lab = QLabel("Opacidad")
        lab.setStyleSheet("color:#cfd8d2; background:transparent;")
        ol.addWidget(lab)
        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(30, 100)
        self.opacity_slider.setFixedWidth(110)
        self.opacity_slider.setValue(int(float(settings.value(geo_key + "_opacity", 100))))
        self.opacity_slider.valueChanged.connect(self._set_opacity)
        ol.addWidget(self.opacity_slider)
        self._chrome.set_bottom(opts)
        self._set_opacity(self.opacity_slider.value())

    def _set_opacity(self, v):
        self.setWindowOpacity(v / 100.0)
        self._settings.setValue(self._geo_key + "_opacity", int(v))

    def set_content(self, widget: QWidget):
        self._lay.addWidget(widget)
        self._chrome.raise_overlays()     # barra y bordes siempre por encima del contenido

    def restore_and_show(self):
        geo = self._settings.value(self._geo_key)
        if geo:
            self.restoreGeometry(geo)
        else:
            self.resize(500, 700)
        # Verificar que la ventana es visible en algún monitor
        center = self.frameGeometry().center()
        for screen in QApplication.screens():
            if screen.availableGeometry().contains(center):
                break
        else:
            primary = QApplication.primaryScreen()
            if primary:
                sg = primary.availableGeometry()
                self.move(sg.center() - self.rect().center())
        self.show()

    def closeEvent(self, e):
        self._settings.setValue(self._geo_key, self.saveGeometry())
        e.accept()
        if not self._suppress_signal:
            self.reattach_requested.emit()


# ---------------------------------------------------------------------------
# Ventana principal
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        # Sin marco de Windows: los botones de ventana van en la barra de herramientas
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.settings = QSettings("Alowster", "WTNav")
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self._icon = make_icon()
        self.setWindowIcon(self._icon)

        self.model = FlightModel()
        try:
            self.model.limits = json.loads(self.settings.value("limits", "{}"))
        except ValueError:
            self.model.limits = {}
        self.model.reserve_min = float(self.settings.value("reserve_min", 3.0))

        self.state, self.ind, self.objs, self.info = {}, {}, [], {}
        self.snap = None
        self._panel_snap = None

        # Ventanas flotantes (None = acoplado en el splitter)
        self._map_window: FloatingWindow | None = None
        self._panel_window: FloatingWindow | None = None

        self.map = MapWidget()
        self.panel = InfoPanel(self.settings)
        # Filtros independientes para el mapa cuadrado y la brújula, guardados entre sesiones
        self.filters_square = MapFilters("square")
        self.filters_circle = MapFilters("circle")
        saved = self.settings.value("filters/square")
        if saved:
            self.filters_square.load_json(saved)
        else:   # primera vez: respetar los interruptores de versiones anteriores
            for old, helper in (("trail", "trail"), ("rings", "rings"),
                                ("enemy_cones", "cones"), ("contacts", "contacts")):
                v = self.settings.value(old)
                if v is not None:
                    self.filters_square.helpers[helper] = v in (True, "true", "1", 1)
        self.filters_circle.load_json(self.settings.value("filters/circle"))
        self.map.set_filters(self.filters_square)
        self._circle_window: CircleMapWindow | None = None
        self._circle_prev_floating = False
        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self.map)
        self.split.addWidget(self.panel)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([760, 330])
        self.setCentralWidget(self.split)

        self._build_toolbar()
        self.statusBar().showMessage("Conectando...")

        self.client = WTClient(self.settings.value("host", "127.0.0.1:8111"), self)
        self.client.stateReceived.connect(lambda d: setattr(self, "state", d))
        self.client.indicatorsReceived.connect(lambda d: setattr(self, "ind", d))
        self.client.mapInfoReceived.connect(self._on_info)
        self.client.mapObjReceived.connect(self._on_objs)
        self.client.mapImageReceived.connect(self.map.set_image)
        self.client.iconFontReceived.connect(self.map.set_icon_font)
        self.client.connectionChanged.connect(self._on_conn)

        self.map.waypointSet.connect(self._set_wp)
        self.map.waypointCleared.connect(self._clear_wp)
        self.map.homePicked.connect(self._set_home)
        self.map.trailSegmentDeleted.connect(self._del_trail)

        # El mapa se repinta solo cuando llegan datos nuevos o cambia la vista (ver MapWidget)
        self.t_panel = QTimer(self)
        self.t_panel.timeout.connect(self._refresh_panel)
        self.t_panel.start(250)

        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
            self._ensure_visible()
        else:
            self.resize(1120, 760)
        sp = self.settings.value("splitter")
        if sp is not None:
            self.split.restoreState(sp)
        self.client.start()

        # Restaurar ventanas flotantes si estaban separadas al cerrar
        def _bool(key):
            v = self.settings.value(key, False)
            return v in (True, "true", "1", 1)
        if _bool("detach_map"):
            self._a_detach_map.setChecked(True)
        if _bool("detach_panel"):
            self._a_detach_panel.setChecked(True)
        if _bool("circle_mode"):
            self._a_circle.setChecked(True)

    # ------------------------------------------------------------------ UI

    def _build_toolbar(self):
        tb = QToolBar("Herramientas")
        tb.setMovable(False)

        def toggle(text, tip, key, default, slot, shortcut=None):
            a = QAction(text, self, checkable=True)
            a.setToolTip(tip + (f"  [{shortcut}]" if shortcut else ""))
            val = self.settings.value(key, default)
            a.setChecked(val in (True, "true", "1", 1))
            a.toggled.connect(lambda on, k=key: (self.settings.setValue(k, on), slot(on)))
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
            tb.addAction(a)
            slot(a.isChecked())
            return a

        self.a_top = toggle("Siempre encima", "Mantener la ventana sobre el juego",
                            "on_top", False, self._set_on_top, "Ctrl+T")
        self.a_follow = toggle("Seguirme", "Centrar el mapa en tu avión",
                               "follow", False,
                               lambda on: (setattr(self.map, "follow", on), self.map.update()), "F")
        a = QAction("Vista", self)
        a.setToolTip("Restablecer zoom  [Home]")
        a.setShortcut(QKeySequence("Home"))
        a.triggered.connect(self.map.reset_view)
        tb.addAction(a)

        a = QAction("Base auto", self)
        a.setToolTip("Volver a elegir la base automáticamente")
        a.triggered.connect(lambda: self._set_home(None))
        tb.addAction(a)

        a = QAction("Límites motor", self)
        a.setToolTip("Configurar temperaturas límite del avión actual")
        a.triggered.connect(self._edit_limits)
        tb.addAction(a)

        toggle("Rumbo ↑", "Girar el mapa para que tu rumbo apunte siempre hacia arriba",
               "heading_up", False, lambda on: (setattr(self.map, "heading_up", on), self.map.update()))

        tb.addSeparator()

        # Separar mapa / panel en ventanas independientes
        self._a_detach_map = QAction("⊞ Mapa", self, checkable=True)
        self._a_detach_map.setToolTip(
            "Separar el mapa en una ventana independiente.\n"
            "Útil para colocar el mapa y el panel en lados opuestos de la pantalla."
        )
        self._a_detach_map.toggled.connect(self._detach_map)
        tb.addAction(self._a_detach_map)

        self._a_detach_panel = QAction("⊞ Panel", self, checkable=True)
        self._a_detach_panel.setToolTip(
            "Separar el panel de datos en una ventana independiente.\n"
            "Cuando la ventana es más ancha que alta, las secciones se distribuyen en columnas."
        )
        self._a_detach_panel.toggled.connect(self._detach_panel)
        tb.addAction(self._a_detach_panel)

        self._a_circle = QAction("◯ Brújula", self, checkable=True)
        self._a_circle.setToolTip(
            "Convertir el mapa en una brújula circular: centrada en ti, con tu rumbo arriba,\n"
            "tamaño y escala ajustables. Se abre en su propia ventana."
        )
        self._a_circle.toggled.connect(self._set_circle)
        tb.addAction(self._a_circle)

        tb.addSeparator()

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        # Opacidad y Host van en el menú ⚙ (fuera de la barra): son ajustes de una vez y, al ser
        # controles y no botones, no pueden pasar al menú » cuando la ventana es estrecha.
        settings_box = QWidget()
        form = QFormLayout(settings_box)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(8)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(30, 100)
        self.opacity.setFixedWidth(150)
        self.opacity.setValue(int(self.settings.value("opacity", 100)))
        self.opacity.valueChanged.connect(self._set_opacity)
        form.addRow("Opacidad", self.opacity)
        self._set_opacity(self.opacity.value())
        self.host = QLineEdit(self.settings.value("host", "127.0.0.1:8111"))
        self.host.setFixedWidth(150)
        self.host.setToolTip("IP:puerto del PC con el juego.")
        self.host.editingFinished.connect(self._set_host)
        form.addRow("Host", self.host)

        def check(text, tip, key, attr):
            cb = QCheckBox(text)
            cb.setToolTip(tip)
            on = self.settings.value(key, True) in (True, "true", "1", 1)
            cb.setChecked(on)
            setattr(self.map, attr, on)
            cb.toggled.connect(lambda v, k=key, a=attr: (self.settings.setValue(k, v),
                                                         setattr(self.map, a, v), self.map.update()))
            form.addRow(cb)
            return cb

        b_filters = QPushButton("Filtros del mapa…")
        b_filters.setToolTip("Qué se muestra en el mapa cuadrado (enemigos, aliados, buques, conos...)")
        b_filters.clicked.connect(lambda: self._open_filters(
            "square", b_filters.mapToGlobal(QPoint(0, b_filters.height() + 4))))
        form.addRow(b_filters)
        menu = QMenu(self)
        wa = QWidgetAction(menu)
        wa.setDefaultWidget(settings_box)
        menu.addAction(wa)
        self._settings_btn = QToolButton()
        self._settings_btn.setText("⚙ Ajustes")
        self._settings_btn.setToolTip("Opacidad de la ventana y dirección del juego")
        self._settings_btn.setPopupMode(QToolButton.InstantPopup)
        self._settings_btn.setMenu(menu)
        self._settings_btn.setObjectName("settingsBtn")

        # Cabecera = barra de herramientas + botones de ventana. Los botones van fuera del
        # QToolBar para que nunca acaben en su menú de desbordamiento (») si falta espacio.
        header = QWidget()
        header.setObjectName("header")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        # Ignored: la barra puede encoger lo que haga falta (lo que no cabe pasa a »)
        # sin invadir el botón ⚙ ni los botones de ventana.
        tb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hl.addWidget(tb, 1)
        hl.addWidget(self._settings_btn, 0, Qt.AlignVCenter)
        hl.addSpacing(6)
        self._caption = CaptionButtons(self, header, height=tb.sizeHint().height())
        hl.addWidget(self._caption, 0, Qt.AlignTop)
        self.setMenuWidget(header)
        # Arrastrar desde los huecos vacíos de la cabecera mueve la ventana
        self._dragger = WindowDragger(self, [header, tb, spacer] +
                                      [w for w in tb.findChildren(QLabel)])
        self._chrome = FramelessChrome(self, overlay_bar=False)   # solo bordes de redimensionado

        # Si la ventana es estrecha, Qt mete lo que no cabe en el botón », pero su flecha
        # por defecto es oscura e invisible sobre este fondo: se le pone un icono claro.
        ext = tb.findChild(QToolButton, "qt_toolbar_ext_button")
        if ext:
            ext.setIcon(_chevron_icon())
            ext.setIconSize(QSize(10, 16))
            ext.setToolTip("Más opciones (no caben en la barra)")

    # ------------------------------------------------------------------ ventanas flotantes

    def _detach_map(self, on: bool):
        if on:
            b_f = QPushButton("Filtros")
            b_f.clicked.connect(lambda: self._open_filters(
                "square", b_f.mapToGlobal(QPoint(0, -10)), above=True))
            b_c = QPushButton("◯ Brújula")
            b_c.setToolTip("Convertir este mapa en brújula circular")
            b_c.clicked.connect(lambda: self._a_circle.setChecked(True))
            fw = FloatingWindow("WT Nav — Mapa", self.settings, "map_float_geo", self._icon,
                                extra_buttons=(b_f, b_c))
            fw.reattach_requested.connect(self._reattach_map)
            fw.set_content(self.map)
            if self._is_on_top():
                fw.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            fw.restore_and_show()
            self._map_window = fw
        else:
            self._do_reattach_map()

    def _reattach_map(self):
        """Llamado cuando el usuario cierra la ventana flotante del mapa."""
        self._do_reattach_map()
        self._a_detach_map.blockSignals(True)
        self._a_detach_map.setChecked(False)
        self._a_detach_map.blockSignals(False)

    def _do_reattach_map(self):
        if self._map_window:
            self._map_window._suppress_signal = True
            self.split.insertWidget(0, self.map)
            self.map.show()
            self._map_window.close()
            self._map_window = None

    def _detach_panel(self, on: bool):
        if on:
            fw = FloatingWindow("WT Nav — Panel", self.settings, "panel_float_geo", self._icon)
            fw.reattach_requested.connect(self._reattach_panel)
            fw.set_content(self.panel)
            if self._is_on_top():
                fw.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            fw.restore_and_show()
            self._panel_window = fw
        else:
            self._do_reattach_panel()

    def _reattach_panel(self):
        """Llamado cuando el usuario cierra la ventana flotante del panel."""
        self._do_reattach_panel()
        self._a_detach_panel.blockSignals(True)
        self._a_detach_panel.setChecked(False)
        self._a_detach_panel.blockSignals(False)

    def _do_reattach_panel(self):
        if self._panel_window:
            self._panel_window._suppress_signal = True
            # Insertar después del mapa (o al final si el mapa está separado)
            idx = 1 if self._map_window is None else self.split.count()
            self.split.insertWidget(idx, self.panel)
            self.panel.show()
            self._panel_window.close()
            self._panel_window = None

    # ------------------------------------------------------------------ brújula y filtros

    def _set_circle(self, on: bool):
        """Mapa cuadrado <-> brújula circular (siempre en su propia ventana)."""
        if on and self._circle_window is None:
            self._circle_prev_floating = self._map_window is not None
            if self._map_window is not None:          # quitar el mapa de su ventana flotante
                fw, self._map_window = self._map_window, None
                fw._suppress_signal = True
                self.map.setParent(None)
                fw.close()
            else:                                     # o del splitter de la principal
                self.map.setParent(None)
            self._a_detach_map.blockSignals(True)
            self._a_detach_map.setChecked(False)
            self._a_detach_map.setEnabled(False)
            self._a_detach_map.blockSignals(False)
            cw = CircleMapWindow(self.settings, self._icon)
            cw.squareRequested.connect(lambda: self._a_circle.setChecked(False))
            cw.filtersRequested.connect(lambda pos: self._open_filters("circle", pos))
            self.map.set_filters(self.filters_circle)
            cw.set_map(self.map)
            if self._is_on_top():
                cw.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            cw.restore_and_show()
            self._circle_window = cw
        elif not on and self._circle_window is not None:
            cw, self._circle_window = self._circle_window, None
            m = cw.take_map()
            cw._suppress_signal = True
            cw.close()
            m.set_filters(self.filters_square)
            self._a_detach_map.setEnabled(True)
            if self._circle_prev_floating:            # volver a donde estaba
                self._a_detach_map.setChecked(True)
            else:
                self.split.insertWidget(0, m)
                m.show()

    def _open_filters(self, mode: str, pos: QPoint, above: bool = False):
        f = self.filters_circle if mode == "circle" else self.filters_square

        def changed():
            self.settings.setValue(f"filters/{mode}", f.to_json())
            self.map.update()

        pop = FilterPopup(f, changed, self)
        if above:
            pos = pos - QPoint(0, pop.height())
        pop.popup_at(pos)

    def _is_on_top(self) -> bool:
        v = self.settings.value("on_top", False)
        return v in (True, "true", "1", 1)

    # ------------------------------------------------------------------ ajustes

    def _set_on_top(self, on):
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        self.show()
        for fw in filter(None, [self._map_window, self._panel_window, self._circle_window]):
            fw.setWindowFlag(Qt.WindowStaysOnTopHint, on)
            fw.show()

    def _set_opacity(self, v):
        # Solo la ventana principal (y lo que lleve dentro): cada ventana separada tiene la suya
        self.setWindowOpacity(v / 100.0)
        self.settings.setValue("opacity", v)

    def _set_host(self):
        h = self.host.text().strip()
        self.settings.setValue("host", h)
        self.client.set_host(h)
        self.map.clear_image()

    def _edit_limits(self):
        ac = self.snap.aircraft if self.snap else ""
        dlg = LimitsDialog(ac, self.model.limits.get(ac, {}), self)
        if dlg.exec():
            self.model.limits[ac] = dlg.values()
            self.settings.setValue("limits", json.dumps(self.model.limits))

    # ------------------------------------------------------------------ datos

    def _on_conn(self, ok):
        self.statusBar().showMessage(
            f"Conectado a {self.client.host}" if ok else
            f"Sin conexión con {self.client.host} — ¿está el juego abierto?")

    def _on_info(self, info):
        self.info = info

    def _on_objs(self, objs):
        self.objs = objs
        self.snap = self.model.update(time.monotonic(), self.state, self.ind, objs, self.info)
        self.map.set_data(self.info, objs, self.snap)

    def _refresh_panel(self):
        if self.snap is None or self.snap is self._panel_snap:
            return          # sin datos nuevos desde el último refresco
        self._panel_snap = self.snap
        self.panel.refresh(self.snap)
        if self.snap.in_game:
            ac = self.snap.aircraft or "?"
            self.statusBar().showMessage(f"Conectado a {self.client.host} · {ac}")

    def _set_wp(self, nx, ny):
        self.model.waypoint = (nx, ny)

    def _clear_wp(self):
        self.model.waypoint = None

    def _del_trail(self, seg_idx: int):
        self.model.remove_trail_segment(seg_idx)

    def _set_home(self, key):
        self.model.home_key = key
        self.model.thr_lock = None

    # ------------------------------------------------------------------ ciclo de vida

    def _ensure_visible(self):
        """Si la ventana restaurada cae fuera de todos los monitores, la centra en la pantalla primaria."""
        center = self.frameGeometry().center()
        for screen in QApplication.screens():
            if screen.availableGeometry().contains(center):
                return
        primary = QApplication.primaryScreen()
        if primary:
            sg = primary.availableGeometry()
            self.move(sg.center() - self.rect().center())

    def closeEvent(self, e):
        # Guardar estado de ventanas flotantes antes de cerrarlas
        self.settings.setValue("detach_map",   self._map_window is not None or
                               (self._circle_window is not None and self._circle_prev_floating))
        self.settings.setValue("detach_panel", self._panel_window is not None)
        self.settings.setValue("circle_mode", self._circle_window is not None)
        for fw in filter(None, [self._map_window, self._panel_window, self._circle_window]):
            fw._suppress_signal = True
            fw.close()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.split.saveState())
        super().closeEvent(e)


# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
