"""Barra de título propia para ventanas sin marco de Windows.

Transparente y discreta: aparece al entrar con el ratón en la ventana y se desvanece al salir.
Permite arrastrar la ventana (movimiento nativo: encaje en bordes y cambio de monitor),
tiene los botones minimizar / maximizar / cerrar, y añade bordes invisibles para redimensionar,
que se pierden al quitar el marco de Windows.

Piezas reutilizables:
  CaptionButtons  - botones minimizar / maximizar / cerrar de una ventana
  WindowDragger   - convierte widgets en "zona de arrastre" de la ventana
  FramelessChrome - bordes de redimensionado y, opcionalmente, la barra flotante transparente

Uso (ventana con Qt.FramelessWindowHint):
  ventanas flotantes:  self._chrome = FramelessChrome(self, "Título")      # barra que aparece/desaparece
                       ... tras añadir el contenido: self._chrome.raise_overlays()
  ventana principal:   self._chrome = FramelessChrome(self, overlay_bar=False)  # solo bordes
                       + CaptionButtons y WindowDragger dentro de su propia cabecera
"""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QPointF, QPropertyAnimation, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QApplication, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QWidget

BAR_H = 28
GRIP = 5            # grosor de los bordes de redimensionado (px)
HIDE_DELAY_MS = 700
FADE_MS = 160


class _CaptionButton(QAbstractButton):
    """Botón dibujado a mano: no depende de iconos ni del tema de Windows."""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind            # "min" | "max" | "close"
        self.maximized = False
        self.setFixedSize(42, BAR_H)
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip({"min": "Minimizar", "max": "Maximizar", "close": "Cerrar (vuelve a la ventana principal)"}[kind])

    def enterEvent(self, e):
        self.update()

    def leaveEvent(self, e):
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hover = self.underMouse()
        if hover:
            bg = QColor(196, 43, 28, 230) if self.kind == "close" else QColor(255, 255, 255, 40)
            if self.isDown():
                bg = bg.darker(120)
            p.fillRect(self.rect(), bg)
        c = QPointF(self.width() / 2, self.height() / 2)
        # Sombra oscura + trazo claro: se distingue sobre cualquier zona del mapa
        for off, col, w in ((1.0, QColor(0, 0, 0, 150), 2.2), (0.0, QColor(255, 255, 255, 235), 1.2)):
            p.setPen(QPen(col, w))
            p.setBrush(Qt.NoBrush)
            d = QPointF(off, off)
            if self.kind == "min":
                p.drawLine(c + QPointF(-5, 0) + d, c + QPointF(5, 0) + d)
            elif self.kind == "max":
                if self.maximized:      # dos cuadrados: restaurar
                    p.drawRect(QRectF(c.x() - 5 + off, c.y() - 3 + off, 8, 8))
                    p.drawLine(c + QPointF(-3, -3) + d, c + QPointF(-3, -5) + d)
                    p.drawLine(c + QPointF(-3, -5) + d, c + QPointF(5, -5) + d)
                    p.drawLine(c + QPointF(5, -5) + d, c + QPointF(5, 3) + d)
                    p.drawLine(c + QPointF(5, 3) + d, c + QPointF(3, 3) + d)
                else:
                    p.drawRect(QRectF(c.x() - 5 + off, c.y() - 5 + off, 10, 10))
            else:
                p.drawLine(c + QPointF(-5, -5) + d, c + QPointF(5, 5) + d)
                p.drawLine(c + QPointF(-5, 5) + d, c + QPointF(5, -5) + d)


def _toggle_max(win: QWidget):
    win.showNormal() if win.isMaximized() else win.showMaximized()


class CaptionButtons(QWidget):
    """Los tres botones de ventana. Sigue solo el estado maximizado para cambiar el icono."""

    def __init__(self, win: QWidget, parent=None, height: int = BAR_H):
        super().__init__(parent)
        self._win = win
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.b_min = _CaptionButton("min", self)
        self.b_max = _CaptionButton("max", self)
        self.b_close = _CaptionButton("close", self)
        for b in (self.b_min, self.b_max, self.b_close):
            b.setFixedHeight(height)
            lay.addWidget(b)
        self.b_min.clicked.connect(win.showMinimized)
        self.b_max.clicked.connect(lambda: _toggle_max(win))
        self.b_close.clicked.connect(win.close)
        win.installEventFilter(self)

    def any_down(self) -> bool:
        return any(b.isDown() for b in (self.b_min, self.b_max, self.b_close))

    def eventFilter(self, obj, e):
        if obj is self._win and e.type() == QEvent.WindowStateChange:
            m = self._win.isMaximized()
            self.b_max.maximized = m
            self.b_max.setToolTip("Restaurar" if m else "Maximizar")
            self.b_max.update()
        return False


class WindowDragger(QObject):
    """Arrastrar sobre los widgets indicados mueve la ventana; doble clic maximiza/restaura.

    Usa el movimiento nativo del sistema (encaje en bordes, cambio de monitor); si no está
    disponible, mueve la ventana a mano.
    """

    def __init__(self, win: QWidget, widgets=()):
        super().__init__(win)
        self._win = win
        self._drag_from: QPoint | None = None
        for w in widgets:
            self.add(w)

    def add(self, w: QWidget):
        w.installEventFilter(self)

    def eventFilter(self, obj, e):
        t = e.type()
        if t == QEvent.MouseButtonPress and e.button() == Qt.LeftButton:
            wh = self._win.windowHandle()
            if wh is None or not wh.startSystemMove():
                self._drag_from = e.globalPosition().toPoint() - self._win.frameGeometry().topLeft()
            return True
        if t == QEvent.MouseMove and self._drag_from is not None:
            if not self._win.isMaximized():
                self._win.move(e.globalPosition().toPoint() - self._drag_from)
            return True
        if t == QEvent.MouseButtonRelease:
            self._drag_from = None
        if t == QEvent.MouseButtonDblClick and e.button() == Qt.LeftButton:
            _toggle_max(self._win)
            return True
        return False


class _TitleBar(QWidget):
    """Barra flotante de las ventanas separadas: título + botones, transparente."""

    def __init__(self, win: QWidget, title: str):
        super().__init__(win)
        self.setFixedHeight(BAR_H)
        self.setCursor(Qt.ArrowCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 0, 0)
        lay.setSpacing(0)
        self.title = QLabel(title)
        self.title.setFont(QFont("Segoe UI", 9))
        self.title.setStyleSheet("color: rgba(232,239,233,200); background: transparent;")
        self.title.setAttribute(Qt.WA_TransparentForMouseEvents)
        lay.addWidget(self.title, 1)
        self.caption = CaptionButtons(win, self)
        lay.addWidget(self.caption)
        self.dragger = WindowDragger(win, [self])

    def paintEvent(self, e):
        # Degradado muy suave para que título y botones se lean sobre el mapa; no es un fondo sólido
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, QColor(0, 0, 0, 120))
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g)


class _Grip(QWidget):
    """Franja invisible en un borde o esquina: redimensiona la ventana con el sistema."""

    CURSORS = {
        Qt.LeftEdge: Qt.SizeHorCursor, Qt.RightEdge: Qt.SizeHorCursor,
        Qt.TopEdge: Qt.SizeVerCursor, Qt.BottomEdge: Qt.SizeVerCursor,
    }

    def __init__(self, win: QWidget, edges):
        super().__init__(win)
        self._win = win
        self.edges = edges
        self._start = None
        diag_a = (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge)
        diag_b = (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge)
        if edges in diag_a:
            self.setCursor(Qt.SizeFDiagCursor)
        elif edges in diag_b:
            self.setCursor(Qt.SizeBDiagCursor)
        else:
            self.setCursor(self.CURSORS[edges])

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        wh = self._win.windowHandle()
        if wh is not None and wh.startSystemResize(self.edges):
            return
        self._start = (e.globalPosition().toPoint(), self._win.geometry())

    def mouseMoveEvent(self, e):        # respaldo manual si el sistema no lo soporta
        if not self._start:
            return
        p0, g0 = self._start
        d = e.globalPosition().toPoint() - p0
        g = g0.adjusted(0, 0, 0, 0)
        if self.edges & Qt.LeftEdge:
            g.setLeft(min(g0.left() + d.x(), g0.right() - self._win.minimumWidth()))
        if self.edges & Qt.RightEdge:
            g.setRight(g0.right() + d.x())
        if self.edges & Qt.TopEdge:
            g.setTop(min(g0.top() + d.y(), g0.bottom() - self._win.minimumHeight()))
        if self.edges & Qt.BottomEdge:
            g.setBottom(g0.bottom() + d.y())
        self._win.setGeometry(g)

    def mouseReleaseEvent(self, e):
        self._start = None


class _BottomBar(QWidget):
    """Franja inferior de opciones (filtros, opacidad...) con degradado suave, como la barra."""

    def __init__(self, win: QWidget, content: QWidget):
        super().__init__(win)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 4, 10, 6)
        lay.addWidget(content)

    def paintEvent(self, e):
        p = QPainter(self)
        g = QLinearGradient(0, 0, 0, self.height())
        g.setColorAt(0.0, QColor(0, 0, 0, 0))
        g.setColorAt(0.45, QColor(0, 0, 0, 150))
        g.setColorAt(1.0, QColor(0, 0, 0, 190))
        p.fillRect(self.rect(), g)


class FramelessChrome(QObject):
    """Barra de título transparente + bordes de redimensionado para una ventana sin marco."""

    def __init__(self, win: QWidget, title: str = "", overlay_bar: bool = True):
        super().__init__(win)
        self._win = win
        self.bar = _TitleBar(win, title)
        self._fx = QGraphicsOpacityEffect(self.bar)
        self._fx.setOpacity(0.0)
        self.bar.setGraphicsEffect(self._fx)
        self.bar.hide()          # oculta no bloquea los clics sobre el contenido de debajo
        self._anim = QPropertyAnimation(self._fx, b"opacity", self)
        self._anim.setDuration(FADE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._anim_done)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(HIDE_DELAY_MS)
        self._hide_timer.timeout.connect(self.fade_out)

        self._overlay = overlay_bar     # False: la ventana tiene su propia cabecera (ventana principal)
        self.bottom = None              # franja inferior de opciones (opcional)
        self._fx_b = None
        self._anim_b = None

        L, R, T, B = Qt.LeftEdge, Qt.RightEdge, Qt.TopEdge, Qt.BottomEdge
        self.grips = [_Grip(win, e) for e in (L, R, T, B, L | T, R | T, L | B, R | B)]
        win.installEventFilter(self)
        self._layout_overlays()

    def set_bottom(self, content: QWidget):
        """Opciones de la ventana bajo el contenido; aparecen y desaparecen con la barra."""
        self.bottom = _BottomBar(self._win, content)
        self._fx_b = QGraphicsOpacityEffect(self.bottom)
        self._fx_b.setOpacity(0.0)
        self.bottom.setGraphicsEffect(self._fx_b)
        self.bottom.hide()
        self._anim_b = QPropertyAnimation(self._fx_b, b"opacity", self)
        self._anim_b.setDuration(FADE_MS)
        self._anim_b.setEasingCurve(QEasingCurve.OutCubic)
        self._anim_b.finished.connect(lambda: self.bottom.hide() if self._fx_b.opacity() <= 0.01 else None)
        self._layout_overlays()

    # -- visibilidad
    def fade_in(self):
        if not self._overlay:
            return
        self._hide_timer.stop()
        if self.bar.isVisible() and self._anim.endValue() == 1.0:
            return
        self.bar.show()
        if self.bottom is not None:
            self.bottom.show()
        self.raise_overlays()
        self._animate(1.0)

    def fade_out(self):
        over_bottom = self.bottom is not None and self.bottom.underMouse()
        popup = QApplication.activePopupWidget() is not None
        if self.bar.underMouse() or over_bottom or popup or self.bar.caption.any_down():
            self._hide_timer.start()          # con el ratón encima, no se esconde
            return
        self._animate(0.0)

    def _animate(self, target: float):
        self._anim.stop()
        self._anim.setStartValue(self._fx.opacity())
        self._anim.setEndValue(target)
        self._anim.start()
        if self._anim_b is not None:
            self._anim_b.stop()
            self._anim_b.setStartValue(self._fx_b.opacity())
            self._anim_b.setEndValue(target)
            self._anim_b.start()

    def _anim_done(self):
        if self._fx.opacity() <= 0.01:
            self.bar.hide()

    def raise_overlays(self):
        self.bar.raise_()
        if self.bottom is not None:
            self.bottom.raise_()
        for g in self.grips:
            g.raise_()

    # -- geometría
    def _layout_overlays(self):
        w, h, m = self._win.width(), self._win.height(), GRIP
        self.bar.setGeometry(0, 0, w, BAR_H)
        if self.bottom is not None:
            bh = self.bottom.sizeHint().height()
            self.bottom.setGeometry(0, h - bh, w, bh)
        L, R, T, B = Qt.LeftEdge, Qt.RightEdge, Qt.TopEdge, Qt.BottomEdge
        rects = {L: (0, m, m, h - 2 * m), R: (w - m, m, m, h - 2 * m), T: (m, 0, w - 2 * m, m),
                 B: (m, h - m, w - 2 * m, m), L | T: (0, 0, m, m), R | T: (w - m, 0, m, m),
                 L | B: (0, h - m, m, m), R | B: (w - m, h - m, m, m)}
        maxed = self._win.isMaximized() or self._win.isFullScreen()
        for g in self.grips:
            g.setGeometry(*rects[g.edges])
            g.setVisible(not maxed)

    def eventFilter(self, obj, e):
        if obj is self._win:
            t = e.type()
            if t == QEvent.Enter:
                self.fade_in()
            elif t == QEvent.Leave:
                self._hide_timer.start()
            elif t == QEvent.Resize:
                self._layout_overlays()
            elif t == QEvent.WindowStateChange:
                self._layout_overlays()
            elif t == QEvent.ChildAdded:
                QTimer.singleShot(0, self.raise_overlays)
        return False
