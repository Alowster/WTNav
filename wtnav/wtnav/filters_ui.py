"""Ventana emergente de filtros del mapa: casillas "Mostrar" y "Borde" por categoría."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QCheckBox, QFrame, QGridLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from .filters import HELPERS, HELPERS_ONLY, OBJECT_CATS, SIDES, SPECIAL, MapFilters

STYLE = """
QFrame#filterPopup { background: #171c19; border: 1px solid #2f3a33; border-radius: 8px; }
QLabel { color: #e8efe9; background: transparent; }
QLabel#hdr { color: #8fb79a; font-size: 8pt; font-weight: bold; letter-spacing: 1px; padding-top: 6px; }
QLabel#col { color: #93a39a; font-size: 8pt; }
QCheckBox { color: #e8efe9; spacing: 6px; background: transparent; }
QCheckBox::indicator { width: 13px; height: 13px; border: 1px solid #6b7d72; border-radius: 3px;
                       background: #1d2420; }
QCheckBox::indicator:hover { border-color: #8fe3a1; }
QCheckBox::indicator:checked { background: #8fe3a1; border-color: #8fe3a1; }
QPushButton { background: #222925; color: #e8efe9; border: 1px solid #333c36; border-radius: 4px; padding: 3px 10px; }
QPushButton:hover { background: #2f5a3d; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
"""


class FilterPopup(QFrame):
    """Emergente que edita un MapFilters. Llama a on_change() en cada cambio (guardar + repintar).

    No se cierra al marcar casillas (un QMenu lo haría a cada clic); se cierra al pulsar fuera.
    """

    def __init__(self, filters: MapFilters, on_change, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("filterPopup")
        self.setStyleSheet(STYLE)
        self.f = filters
        self.on_change = on_change
        circle = filters.mode == "circle"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 10)
        title = QLabel("<b>Filtros del " + ("mapa brújula" if circle else "mapa") + "</b>")
        outer.addWidget(title)

        body = QWidget()
        g = QGridLayout(body)
        g.setContentsMargins(0, 0, 0, 0)
        g.setHorizontalSpacing(14)
        g.setVerticalSpacing(3)
        row = 0

        def header(text, cols=True):
            nonlocal row
            h = QLabel(text.upper())
            h.setObjectName("hdr")
            g.addWidget(h, row, 0)
            if cols:
                for c, t in ((1, "Mostrar"), (2, "Borde" if circle else "")):
                    if t:
                        lab = QLabel(t)
                        lab.setObjectName("col")
                        g.addWidget(lab, row, c, Qt.AlignHCenter)
            row += 1

        def check(dic, key, col, tip=None):
            cb = QCheckBox()
            cb.setChecked(bool(dic.get(key, True)))
            if tip:
                cb.setToolTip(tip)
            cb.toggled.connect(lambda v, d=dic, k=key: self._set(d, k, v))
            g.addWidget(cb, row, col, Qt.AlignHCenter)
            return cb

        header("Bando", cols=False)
        for key, text in SIDES:
            g.addWidget(QLabel(text), row, 0)
            check(self.f.sides, key, 1)
            row += 1

        header("Objetos")
        for key, text, can_rim, _, _ in OBJECT_CATS:
            g.addWidget(QLabel(text), row, 0)
            check(self.f.show, key, 1)
            if circle and can_rim:
                check(self.f.rim, key, 2, "Si queda fuera del radio, fijarlo al borde del círculo")
            row += 1

        header("Tuyo")
        for key, text, can_rim, _, _ in SPECIAL:
            g.addWidget(QLabel(text), row, 0)
            check(self.f.show, key, 1)
            if circle and can_rim:
                check(self.f.rim, key, 2, "Si queda fuera del radio, fijarlo al borde del círculo")
            row += 1

        header("Ayudas", cols=False)
        for key, text, _, _ in HELPERS:
            only = HELPERS_ONLY.get(key)
            if only and only != self.f.mode:
                continue
            g.addWidget(QLabel(text), row, 0)
            check(self.f.helpers, key, 1)
            row += 1

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        btns = QWidget()
        bl = QGridLayout(btns)
        bl.setContentsMargins(0, 6, 0, 0)
        b_all = QPushButton("Mostrar todo")
        b_all.clicked.connect(self._show_all)
        bl.addWidget(b_all, 0, 0)
        b_close = QPushButton("Cerrar")
        b_close.clicked.connect(self.close)
        bl.addWidget(b_close, 0, 1)
        outer.addWidget(btns)

        body.adjustSize()
        self.resize(body.sizeHint().width() + 46, min(560, body.sizeHint().height() + 100))
        self._checks_body = body

    def _set(self, dic, key, value):
        dic[key] = bool(value)
        self.on_change()

    def _show_all(self):
        for d in (self.f.sides, self.f.show):
            for k in d:
                d[k] = True
        self.on_change()
        # reconstruir el estado visual
        pos = self.pos()
        self.close()
        FilterPopup(self.f, self.on_change, self.parent()).popup_at(pos)

    def popup_at(self, global_pos: QPoint):
        """Muestra el emergente en esa posición, sin salirse de la pantalla."""
        scr = self.screen().availableGeometry() if self.screen() else None
        x, y = global_pos.x(), global_pos.y()
        if scr:
            x = max(scr.left(), min(x, scr.right() - self.width()))
            y = max(scr.top(), min(y, scr.bottom() - self.height()))
        self.move(x, y)
        self.show()
