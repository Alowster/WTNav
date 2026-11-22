"""Ventana del mapa brújula: circular, transparente fuera del círculo, sin marco.

- Arrastrar en cualquier punto del círculo mueve la ventana (la brújula no es interactiva).
- Al pasar el ratón aparece debajo una tarjeta con: tamaño, escala (radio), opacidad,
  filtros y el botón para volver al mapa cuadrado. Oculta, esa zona es transparente.
- Tamaño, radio, opacidad y posición se guardan entre sesiones.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QEvent, QPoint, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout,
                               QLabel, QPushButton, QSlider, QWidget)

MIN_D, MAX_D = 150, 800
MIN_KM, MAX_KM = 1.0, 100.0
GAP = 6
STRIP_W = 270

STRIP_STYLE = """
QFrame#circleStrip { background: rgba(23, 28, 25, 230); border: 1px solid #2f3a33; border-radius: 8px; }
QLabel { color: #cfd8d2; background: transparent; font-size: 9pt; }
QLabel#val { color: #8fe3a1; font-weight: 600; }
QPushButton { background: #222925; color: #e8efe9; border: 1px solid #333c36; border-radius: 4px;
              padding: 3px 10px; font-size: 9pt; }
QPushButton:hover { background: #2f5a3d; }
QSlider::groove:horizontal { height: 4px; background: #333c36; border-radius: 2px; }
QSlider::handle:horizontal { background: #8fe3a1; width: 12px; margin: -5px 0; border-radius: 6px; }
"""


def km_to_slider(km: float) -> int:
    return int(round(1000 * math.log(km / MIN_KM) / math.log(MAX_KM / MIN_KM)))


def slider_to_km(v: int) -> float:
    return MIN_KM * (MAX_KM / MIN_KM) ** (v / 1000.0)


class CircleMapWindow(QWidget):
    squareRequested = Signal()          # volver al mapa cuadrado
    filtersRequested = Signal(QPoint)   # abrir filtros de la brújula en esa posición

    def __init__(self, settings, icon=None):
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("WT Nav — Brújula")
        if icon:
            self.setWindowIcon(icon)
        self._s = settings
        self._map = None
        self._suppress_signal = False
        self.diameter = int(float(settings.value("circle/diameter", 320)))
        self.radius_km = float(settings.value("circle/radius_km", 20.0))

        # ---- tarjeta de controles
        self.strip = QFrame(self)
        self.strip.setObjectName("circleStrip")
        self.strip.setStyleSheet(STRIP_STYLE)
        g = QGridLayout(self.strip)
        g.setContentsMargins(10, 8, 10, 8)
        g.setHorizontalSpacing(8)
        g.setVerticalSpacing(4)

        def slider(row, text, lo, hi, val, slot):
            g.addWidget(QLabel(text), row, 0)
            sl = QSlider(Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setValue(val)
            g.addWidget(sl, row, 1)
            lab = QLabel()
            lab.setObjectName("val")
            lab.setMinimumWidth(52)
            g.addWidget(lab, row, 2)
            sl.valueChanged.connect(slot)
            return sl, lab

        self.sl_size, self.lb_size = slider(0, "Tamaño", MIN_D, MAX_D, self.diameter, self._on_size)
        self.sl_scale, self.lb_scale = slider(1, "Escala", 0, 1000, km_to_slider(self.radius_km), self._on_scale)
        op = int(float(settings.value("circle/opacity", 100)))
        self.sl_op, self.lb_op = slider(2, "Opacidad", 30, 100, op, self._on_opacity)

        btns = QWidget()
        bl = QHBoxLayout(btns)
        bl.setContentsMargins(0, 2, 0, 0)
        b_f = QPushButton("Filtros")
        b_f.clicked.connect(lambda: self.filtersRequested.emit(
            self.strip.mapToGlobal(QPoint(0, self.strip.height() + 4))))
        b_sq = QPushButton("◻ Mapa cuadrado")
        b_sq.setToolTip("Volver al mapa cuadrado")
        b_sq.clicked.connect(self.close)
        bl.addWidget(b_f)
        bl.addStretch(1)
        bl.addWidget(b_sq)
        g.addWidget(btns, 3, 0, 1, 3)
        self.strip.setFixedWidth(STRIP_W)
        self.strip.adjustSize()

        self._fx = QGraphicsOpacityEffect(self.strip)
        self._fx.setOpacity(0.0)
        self.strip.setGraphicsEffect(self._fx)
        self.strip.hide()
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(lambda: self.strip.hide() if self._fx.opacity() < 0.01 else None)
        self._hide = QTimer(self)
        self._hide.setSingleShot(True)
        self._hide.setInterval(700)
        self._hide.timeout.connect(self._fade_out)

        self._on_opacity(op)
        self._update_labels()
        self._relayout(keep_center=False)

    # ------------------------------------------------------------------ mapa
    def set_map(self, map_widget):
        self._map = map_widget
        map_widget.setParent(self)
        map_widget.set_circular(True)
        map_widget.set_radius_km(self.radius_km)
        map_widget.radiusChanged.connect(self._on_wheel_radius)
        map_widget.show()
        self._relayout(keep_center=False)
        self.strip.raise_()

    def take_map(self):
        m = self._map
        if m is not None:
            try:
                m.radiusChanged.disconnect(self._on_wheel_radius)
            except (RuntimeError, TypeError):
                pass
            m.set_circular(False)
            m.setParent(None)
        self._map = None
        return m

    # ------------------------------------------------------------------ geometría
    def _relayout(self, keep_center=True):
        D = self.diameter
        sh = self.strip.sizeHint().height()
        W = max(D, STRIP_W)
        H = D + GAP + sh
        old_center = self.mapToGlobal(QPoint(self._circle_x() + self._cur_d() // 2, self._cur_d() // 2)) \
            if keep_center and self.isVisible() else None
        self.setFixedSize(W, H)
        if self._map is not None:
            self._map.setGeometry((W - D) // 2, 0, D, D)
        self.strip.setGeometry((W - STRIP_W) // 2, D + GAP, STRIP_W, sh)
        self._cx, self._cd = (W - D) // 2, D
        if old_center is not None:                  # al cambiar de tamaño el círculo no se mueve
            self.move(old_center - QPoint(self._cx + D // 2, D // 2))

    def _circle_x(self):
        return getattr(self, "_cx", 0)

    def _cur_d(self):
        return getattr(self, "_cd", self.diameter)

    def restore_and_show(self):
        pos = self._s.value("circle/pos")
        if isinstance(pos, QPoint):
            self.move(pos)
        else:
            scr = QApplication.primaryScreen()
            if scr:
                g = scr.availableGeometry()
                self.move(g.right() - self.width() - 40, g.top() + 60)
        # que el círculo caiga en algún monitor
        c = self.frameGeometry().topLeft() + QPoint(self._cx + self.diameter // 2, self.diameter // 2)
        if not any(s.availableGeometry().contains(c) for s in QApplication.screens()):
            scr = QApplication.primaryScreen()
            if scr:
                self.move(scr.availableGeometry().center() - QPoint(self.width() // 2, self.height() // 2))
        self.show()

    # ------------------------------------------------------------------ controles
    def _update_labels(self):
        self.lb_size.setText(f"{self.diameter} px")
        self.lb_scale.setText(f"{self.radius_km:.1f} km" if self.radius_km < 10 else f"{self.radius_km:.0f} km")
        self.lb_op.setText(f"{self.sl_op.value()} %")

    def _on_size(self, v):
        self.diameter = int(v)
        self._s.setValue("circle/diameter", self.diameter)
        self._relayout(keep_center=True)
        self._update_labels()

    def _on_scale(self, v):
        self.radius_km = slider_to_km(v)
        self._s.setValue("circle/radius_km", self.radius_km)
        if self._map is not None:
            self._map.set_radius_km(self.radius_km)
        self._update_labels()

    def _on_wheel_radius(self, km):
        self.radius_km = km
        self._s.setValue("circle/radius_km", km)
        self.sl_scale.blockSignals(True)
        self.sl_scale.setValue(km_to_slider(max(MIN_KM, min(MAX_KM, km))))
        self.sl_scale.blockSignals(False)
        self._update_labels()

    def _on_opacity(self, v):
        self.setWindowOpacity(v / 100.0)
        self._s.setValue("circle/opacity", int(v))
        self._update_labels()

    # ------------------------------------------------------------------ aparecer / desaparecer
    def enterEvent(self, e):
        self._hide.stop()
        if not self.strip.isVisible() or self._anim.endValue() != 1.0:
            self.strip.show()
            self._animate(1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hide.start()
        super().leaveEvent(e)

    def _fade_out(self):
        if self.strip.underMouse() or (self._map is not None and self._map.underMouse()):
            self._hide.start()
            return
        self._animate(0.0)

    def _animate(self, target):
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(target)
        self._anim.start()

    # ------------------------------------------------------------------ cierre
    def closeEvent(self, e):
        self._s.setValue("circle/pos", self.pos())
        e.accept()
        if not self._suppress_signal:
            self.squareRequested.emit()
