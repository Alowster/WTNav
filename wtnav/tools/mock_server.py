"""Servidor falso del puerto 8111 con un vuelo simulado, para probar WT Nav sin el juego.

Uso:  python tools/mock_server.py [puerto]
"""
import io
import json
import math
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SIZE = 65536.0
T0 = time.monotonic()
WIND = (8.0, 0.0)          # m/s hacia el este (viene del oeste)
HOME = (0.25 * SIZE, 0.75 * SIZE)
ELEV = 120.0


def make_map_png():
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return b""
    rnd = random.Random(4)
    im = Image.new("RGB", (512, 512), (92, 112, 78))
    d = ImageDraw.Draw(im)
    for _ in range(140):
        x, y, r = rnd.randint(0, 512), rnd.randint(0, 512), rnd.randint(8, 60)
        c = rnd.choice([(80, 100, 66), (110, 126, 88), (128, 122, 96), (70, 92, 70)])
        d.ellipse((x - r, y - r, x + r, y + r), fill=c)
    d.line([(0, 300), (140, 260), (260, 330), (512, 280)], fill=(70, 100, 140), width=10)
    im = im.filter(ImageFilter.GaussianBlur(3))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


MAP_PNG = make_map_png()


class Sim:
    def __init__(self):
        self.lock = threading.Lock()
        self.x, self.y = HOME[0] - 600, HOME[1]
        self.h = ELEV
        self.hdg = 90.0
        self.tas = 0.0
        self.vy = 0.0
        self.fuel = 320.0
        self.water = 85.0
        self.oil = 70.0
        self.thr = 100.0
        self.t = 0.0
        # enemigos: [x, y, rumbo, velocidad, giro °/s, icono]
        self.enemies = {
            "circulo": [0.29 * SIZE, 0.70 * SIZE, 90.0, 260.0, 6.0, "Fighter"],
            "zigzag": [0.36 * SIZE, 0.74 * SIZE, 300.0, 240.0, 0.0, "Fighter"],
            "bombardero": [0.40 * SIZE, 0.62 * SIZE, 225.0, 150.0, 0.0, "Bomber"],
            "intermitente": [0.17 * SIZE, 0.69 * SIZE, 80.0, 230.0, 0.0, "Fighter"],
        }

    def step(self, dt):
        self.t += dt
        t = self.t
        if t < 5:                                   # en pista
            self.tas, self.vy, self.thr = 0.0, 0.0, 100.0
        elif t < 25:                                # subida con WEP rumbo 060
            self.thr = 110.0
            self.hdg = 60.0 if t > 9 else 90.0
            self.tas = min(120.0, self.tas + 15 * dt)
            self.vy = 18.0 if self.tas > 60 else 0.0
            self.water += 0.25 * dt
            self.oil += 0.12 * dt
        elif t < 40:                                # giro cerrado perdiendo energía
            self.thr = 100.0
            self.hdg = (self.hdg + 9.0 * dt) % 360
            self.tas = max(80.0, self.tas - 1.6 * dt)
            self.vy = -2.0
            self.water -= 0.05 * dt
        else:                                       # motor a ralentí, planeando hacia casa
            self.thr = 0.0
            b = math.degrees(math.atan2(HOME[0] - self.x, -(HOME[1] - self.y))) % 360
            d = (b - self.hdg + 540) % 360 - 180
            self.hdg = (self.hdg + max(-3 * dt, min(3 * dt, d))) % 360
            self.tas = 85.0
            self.vy = -5.0
            self.water -= 0.1 * dt
        z = self.enemies["zigzag"]
        z[4] = 14.0 * math.sin(t / 3.0)
        for e in self.enemies.values():
            e[2] = (e[2] + e[4] * dt) % 360
            rr = math.radians(e[2])
            e[0] += math.sin(rr) * e[3] * dt
            e[1] += -math.cos(rr) * e[3] * dt
        burn = 0.18 if self.thr > 100 else 0.12 if self.thr > 5 else 0.02
        if self.tas > 0:
            self.fuel = max(0.0, self.fuel - burn * dt)
        r = math.radians(self.hdg)
        vh = math.sqrt(max(self.tas ** 2 - self.vy ** 2, 0))
        wx, wy = WIND if self.tas > 0 else (0, 0)
        self.x += (vh * math.sin(r) + wx) * dt
        self.y += (-vh * math.cos(r) + wy) * dt
        self.h = max(ELEV, self.h + self.vy * dt)

    def run(self):
        last = time.monotonic()
        while True:
            time.sleep(0.05)
            now = time.monotonic()
            with self.lock:
                self.step(now - last)
            last = now


SIM = Sim()


def af(cx, cy, hdg, length, color):
    r = math.radians(hdg)
    dx, dy = math.sin(r) * length / 2, -math.cos(r) * length / 2
    return {"type": "airfield", "color": color,
            "sx": (cx - dx) / SIZE, "sy": (cy - dy) / SIZE, "ex": (cx + dx) / SIZE, "ey": (cy + dy) / SIZE}


def map_obj():
    s = SIM
    r = math.radians(s.hdg)
    t = time.monotonic() - T0
    objs = [
        af(HOME[0], HOME[1], 90, 1800, "#174DFF"),
        af(0.78 * SIZE, 0.22 * SIZE, 140, 1600, "#FA0C00"),
        {"type": "bombing_point", "icon": "bombing_point", "color": "#FA0C00", "color[]": [250, 12, 0],
         "blink": 0, "x": 0.55, "y": 0.42},
        {"type": "bombing_point", "icon": "bombing_point", "color": "#FA0C00", "color[]": [250, 12, 0],
         "blink": 1, "x": 0.58, "y": 0.45},
        {"type": "ground_model", "icon": "Airdefence", "color": "#FA0C00", "color[]": [250, 12, 0],
         "blink": 0, "x": 0.52, "y": 0.47},
        {"type": "aircraft", "icon": "Fighter", "color": "#174DFF", "color[]": [23, 77, 255], "blink": 0,
         "x": 0.30, "y": 0.62, "dx": 0.5, "dy": -0.86},
        {"type": "aircraft", "icon": "Player", "color": "#faC81E", "color[]": [250, 200, 30], "blink": 0,
         "x": s.x / SIZE, "y": s.y / SIZE, "dx": math.sin(r), "dy": -math.cos(r)},
    ]
    for name, (x, y, h, v, w, icon) in s.enemies.items():
        if name == "intermitente" and int(s.t / 6) % 2 == 1:
            continue                       # 6 s visible, 6 s sin detectar
        rr = math.radians(h)
        objs.append({"type": "aircraft", "icon": icon, "color": "#f00C00", "color[]": [240, 12, 0],
                     "blink": 0, "icon_bg": "FighterTarget" if name == "circulo" else "none",
                     "x": x / SIZE, "y": y / SIZE, "dx": math.sin(rr), "dy": -math.cos(rr)})
    return objs


def state():
    s = SIM
    return {
        "valid": True, "H, m": round(s.h), "TAS, km/h": round(s.tas * 3.6), "IAS, km/h": round(s.tas * 3.6 * 0.9),
        "M": round(s.tas / 330, 2), "AoA, deg": 3.0, "AoS, deg": 0.0, "Ny": 1.0 if s.t < 25 or s.t > 40 else 2.8,
        "Vy, m/s": round(s.vy, 1), "Wx, deg/s": 0, "Mfuel, kg": round(s.fuel), "Mfuel0, kg": 400,
        "throttle 1, %": round(s.thr), "RPM throttle 1, %": 100, "radiator 1, %": 50,
        "power 1, hp": 1600.0 if s.thr > 5 else 0.0, "RPM 1": 2700,
        "water temp 1, C": round(s.water), "oil temp 1, C": round(s.oil),
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype="application/json"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        with SIM.lock:
            if p == "/map_info.json":
                d = {"grid_steps": [8192, 8192], "grid_zero": [0, SIZE], "grid_size": [SIZE, SIZE],
                     "hud_type": 0, "map_generation": 1, "map_max": [SIZE, SIZE], "map_min": [0, 0], "valid": True}
            elif p == "/map_obj.json":
                d = map_obj()
            elif p == "/state":
                d = state()
            elif p == "/indicators":
                d = {"valid": True, "army": "air", "type": "p-51d-5", "compass": SIM.hdg}
            elif p == "/map.img":
                return self._send(MAP_PNG, "image/png")
            else:
                self.send_response(404)
                self.end_headers()
                return
        self._send(json.dumps(d).encode())


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8111
    threading.Thread(target=SIM.run, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
