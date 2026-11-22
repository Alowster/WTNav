"""Seguidor de aviones enemigos y predicción de su trayectoria. Python puro (sin Qt).

El JSON del juego no da un identificador por avión: solo una lista de posiciones cada ~200 ms.
Para saber cuánto gira y a qué velocidad va cada enemigo hay que reconocer que el punto de
ahora es el mismo avión que el de hace un instante. Esto hace un seguidor de blancos sencillo:

  1. Asociación: cada observación nueva se empareja con el blanco cuya posición PREDICHA
     queda más cerca (mismo tipo de icono, dentro de una puerta de distancia razonable).
  2. Estimación por blanco, con regresión sobre ventanas temporales:
       - velocidad sobre el suelo (posición frente al tiempo)
       - régimen de giro (rumbo dx/dy frente al tiempo)
       - "nerviosismo": cuánto ha variado ese giro en los últimos segundos
  3. Memoria: un blanco que deja de verse se mantiene MEMORY_S segundos como "perdido".
     Si reaparece cerca de donde podría estar, recupera su identidad (y su historial).

Predicción del cono ("dónde puede estar en los próximos T segundos"):
  - Se simulan trayectorias con régimen de giro constante entre ω-Δω y ω+Δω.
  - Largo  = velocidad x T            (más rápido -> cono más largo)
  - Curva  = ω actual                  (si está girando, el cono se dobla con él)
  - Ancho  = Δω, que crece con:
        · un margen base por tipo (un caza puede empezar a girar en cualquier momento)
        · el giro que ya lleva (quien está girando puede apretar o soltar el giro)
        · su nerviosismo reciente (quien lleva rato maniobrando es impredecible)
        · el tiempo que lleva perdido (cuanto más sin verlo, menos sabemos)
    y se limita a lo que físicamente puede girar ese tipo de avión.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .calc import AngleUnwrap, SlopeWindow, bearing, side_of, unit_from_bearing

MEMORY_S = 30.0          # segundos que se recuerda un contacto perdido
NEW_PULSE_S = 2.5        # duración del aviso visual de contacto nuevo
MAX_SPEED = 750.0        # m/s, para la puerta de asociación (por encima de Mach 2 a baja cota)
MAX_TURN_DEG = 90.0      # cada trayectoria del cono se corta al acumular este giro: el cono
                         # muestra la próxima maniobra, no un bucle completo (que no informa nada)

# Parámetros por tipo de icono: horizonte de predicción, margen base de giro, giro máximo
PROFILES = {
    "Fighter": dict(horizon=20.0, base_dw=2.5, max_w=22.0),
    "Assault": dict(horizon=20.0, base_dw=2.0, max_w=18.0),
    "Bomber":  dict(horizon=30.0, base_dw=1.0, max_w=8.0),
    "Helicopter": dict(horizon=20.0, base_dw=4.0, max_w=30.0),
}
DEFAULT_PROFILE = PROFILES["Fighter"]


@dataclass
class Track:
    tid: int
    icon: str
    x: float
    y: float
    heading: float                     # grados de brújula
    first_seen: float
    last_seen: float
    is_target: bool = False            # icon_bg *Target: es el blanco que tienes fijado
    speed: float | None = None         # m/s sobre el suelo
    turn: float | None = None          # °/s, + = derecha
    turn_sigma: float = 0.0            # variabilidad reciente del giro (°/s)
    w_x: SlopeWindow = field(default_factory=lambda: SlopeWindow(3.0))
    w_y: SlopeWindow = field(default_factory=lambda: SlopeWindow(3.0))
    w_h: SlopeWindow = field(default_factory=lambda: SlopeWindow(2.0))
    w_turn: SlopeWindow = field(default_factory=lambda: SlopeWindow(6.0, min_frac=0.0))
    unwrap: AngleUnwrap = field(default_factory=AngleUnwrap)

    @property
    def profile(self) -> dict:
        return PROFILES.get(self.icon, DEFAULT_PROFILE)

    def lost_for(self, now: float) -> float:
        return max(0.0, now - self.last_seen)

    def visible(self, now: float, tol: float = 0.6) -> bool:
        return self.lost_for(now) <= tol

    def predict(self, now: float) -> tuple[float, float]:
        """Posición estimada ahora (para asociar), avanzando en línea recta desde la última vista."""
        dt = self.lost_for(now)
        if not self.speed:
            return self.x, self.y
        ux, uy = unit_from_bearing(self.heading)
        return self.x + ux * self.speed * dt, self.y + uy * self.speed * dt

    def observe(self, now, x, y, heading, is_target):
        if self.lost_for(now) > 2.0:          # vuelve tras perderlo: la ventana vieja no vale
            self.w_x.reset(); self.w_y.reset(); self.w_h.reset(); self.unwrap.reset()
        self.x, self.y, self.heading, self.is_target = x, y, heading, is_target
        self.last_seen = now
        self.w_x.add(now, x)
        self.w_y.add(now, y)
        self.w_h.add(now, self.unwrap(heading))
        vx, vy = self.w_x.slope(), self.w_y.slope()
        if vx is not None and vy is not None:
            self.speed = math.hypot(vx, vy)
        tr = self.w_h.slope()
        if tr is not None:
            self.turn = tr
            self.w_turn.add(now, tr)
            n = len(self.w_turn.buf)
            if n >= 3:
                m = sum(v for _, v in self.w_turn.buf) / n
                self.turn_sigma = math.sqrt(sum((v - m) ** 2 for _, v in self.w_turn.buf) / n)

    # -- cono de predicción
    def turn_band(self, now: float) -> tuple[float, float, float]:
        """(ω central, ω mínimo, ω máximo) en °/s para la predicción."""
        p = self.profile
        w = self.turn or 0.0
        dw = p["base_dw"] + 0.5 * abs(w) + 1.5 * self.turn_sigma
        dw += 0.15 * self.lost_for(now)                  # perdido: incertidumbre creciente
        lo = max(-p["max_w"], w - dw)
        hi = min(p["max_w"], w + dw)
        return max(lo, min(hi, w)), lo, hi

    def cone(self, now: float, steps: int = 14) -> dict | None:
        """Polígono del cono en metros + trayectoria central y marcas cada 5 s.

        Para un contacto perdido, el cono sale de su última posición conocida y cubre
        desde entonces hasta 'ahora + horizonte': la zona donde puede estar ya y en breve.
        """
        if not self.speed or self.speed < 15:
            return None
        T = self.profile["horizon"] + self.lost_for(now)
        w, lo, hi = self.turn_band(now)

        def path(omega):
            pts, h, x, y = [(self.x, self.y)], self.heading, self.x, self.y
            t_end = T if abs(omega) < 1e-6 else min(T, MAX_TURN_DEG / abs(omega))
            dt = t_end / steps
            for _ in range(steps):
                # integración por el punto medio: suficiente para curvas suaves
                hm = h + omega * dt / 2
                ux, uy = unit_from_bearing(hm)
                x += ux * self.speed * dt
                y += uy * self.speed * dt
                h += omega * dt
                pts.append((x, y))
            return pts

        left, right = path(lo), path(hi)
        end_arc = [path(lo + (hi - lo) * k / 6)[-1] for k in range(1, 6)]
        poly = left + end_arc + right[::-1]
        center = path(w)
        ticks = []
        t_center = T if abs(w) < 1e-6 else min(T, MAX_TURN_DEG / abs(w))
        for sec in range(5, int(t_center) + 1, 5):
            i = round(sec / t_center * steps)
            if 0 < i < len(center):
                ticks.append((sec, center[i]))
        return {"poly": poly, "center": center, "ticks": ticks, "horizon": T}


class EnemyTracker:
    def __init__(self):
        self.tracks: dict[int, Track] = {}
        self._next = 1

    def reset(self):
        self.tracks.clear()

    def update(self, now: float, objs: list, size_x: float, size_y: float) -> list[Track]:
        obs = []
        for o in objs:
            if o.get("type") != "aircraft" or o.get("icon") == "Player" or "x" not in o:
                continue
            if side_of(o) != "enemy":
                continue
            dx, dy = o.get("dx", 0.0), o.get("dy", 0.0)
            if not dx and not dy:
                continue
            obs.append((o["x"] * size_x, o["y"] * size_y, bearing(dx, dy), o.get("icon", "?"),
                        str(o.get("icon_bg", "")).endswith("Target")))

        # Emparejamiento voraz por coste creciente: distancia a la posición predicha,
        # dentro de una puerta que crece con el tiempo que lleva sin verse
        pairs = []
        for oi, (x, y, h, icon, _) in enumerate(obs):
            for tid, t in self.tracks.items():
                if t.icon != icon:
                    continue
                dt = t.lost_for(now)
                gate = 300.0 + MAX_SPEED * max(dt, 0.25)
                if t.speed:
                    gate = min(gate, 300.0 + max(MAX_SPEED * 0.5, t.speed * 2.0) * max(dt, 0.25))
                px, py = t.predict(now)
                d = math.hypot(x - px, y - py)
                if d <= gate:
                    pairs.append((d, oi, tid))
        pairs.sort()
        used_o, used_t = set(), set()
        for d, oi, tid in pairs:
            if oi in used_o or tid in used_t:
                continue
            used_o.add(oi)
            used_t.add(tid)
            x, y, h, icon, tgt = obs[oi]
            self.tracks[tid].observe(now, x, y, h, tgt)

        for oi, (x, y, h, icon, tgt) in enumerate(obs):
            if oi in used_o:
                continue
            t = Track(self._next, icon, x, y, h, first_seen=now, last_seen=now)
            t.observe(now, x, y, h, tgt)
            self.tracks[self._next] = t
            self._next += 1

        for tid in [tid for tid, t in self.tracks.items() if t.lost_for(now) > MEMORY_S]:
            del self.tracks[tid]
        return list(self.tracks.values())
