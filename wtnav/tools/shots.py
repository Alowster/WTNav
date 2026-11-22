"""Arranca la app offscreen contra el mock y guarda capturas + valores calculados."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from wtnav.main import STYLE, MainWindow

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots"
TIMES = [float(x) for x in (sys.argv[2:] or ["4", "20", "36", "58"])]
os.makedirs(OUT, exist_ok=True)

app = QApplication(sys.argv)
app.setStyleSheet(STYLE)
w = MainWindow()
w.resize(1180, 780)
w.show()


def shot(t):
    s = w.snap
    w.grab().save(f"{OUT}/t{int(t):03d}.png")
    if s:
        print(f"t={t:.0f} in_game={s.in_game} gs={s.gs and s.gs*3.6:.0f} track={s.track} hdg={s.heading} "
              f"wind={s.wind_speed} from={s.wind_from} ps={s.ps} turn={s.turn_rate} r={s.turn_radius} "
              f"burn={s.burn} endu={s.endurance} margin={s.bingo_margin_s} glide={s.glide_ratio} "
              f"reach={s.glide_reach} home_ok={s.glide_home_ok} wep={s.wep_now:.0f}/{s.wep_total:.0f} "
              f"elev={s.field_elev} known={s.field_elev_known} app_along={s.app_along} lat={s.app_lateral} "
              f"dev={s.app_dev} eng={[(e.water_rate, e.oil_rate) for e in s.engines]} alerts={s.alerts}",
              flush=True)


for t in TIMES:
    QTimer.singleShot(int(t * 1000), lambda t=t: shot(t))
QTimer.singleShot(int(TIMES[-1] * 1000) + 500, app.quit)


def mark():   # marcar un objetivo a mitad del vuelo
    w.model.waypoint = (0.55, 0.42)


QTimer.singleShot(12000, mark)


def zoom():
    w.map.scale = 6.0
    w.map.follow = True


QTimer.singleShot(44000, zoom)
app.exec()
