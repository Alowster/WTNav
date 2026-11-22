"""Motor de cálculos. Python puro (sin Qt) para poder testearlo aislado.

Convenciones de coordenadas:
  - El juego da posiciones normalizadas 0..1 (x hacia el este, y hacia el SUR).
  - Las pasamos a metros multiplicando por el tamaño del mapa (map_max - map_min).
  - Rumbos en grados de brújula: 0 = norte (arriba del mapa), 90 = este.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

G = 9.80665
GLIDE_SLOPE_DEG = 3.0
THRESHOLD_CROSS_M = 15.0
ENTRY_POINT_M = 8000.0
TARGET_ICONS = {
    "bombing_point", "capture_zone", "defending_point", "waypoint", "Structure",
}
# Antiaéreos tal como los llama el juego (vistos en partidas reales; "Airdefence" es el nombre
# antiguo de la página web de Gaijin y se conserva por si algún modo lo sigue usando)
AA_ICONS = {"SAM", "SPAA", "Airdefence"}
# Barcos: Frigate, Destroyer, MissileDestroyer y Boat vistos en partidas reales; el resto de
# palabras cubren variantes con el mismo patrón de nombre
SHIP_WORDS = ("Destroyer", "Frigate", "Boat", "Cruiser", "Battleship", "Ship")
MIN_HOME_RUNWAY_M = 1000.0   # la base automática ignora pistas más cortas (portaaviones, pistas auxiliares)


# --------------------------------------------------------------------------- utilidades

def parse_color(obj: dict) -> tuple[int, int, int] | None:
    c = obj.get("color[]")
    if isinstance(c, (list, tuple)) and len(c) >= 3:
        return int(c[0]), int(c[1]), int(c[2])
    s = obj.get("color")
    if isinstance(s, str) and s.startswith("#") and len(s) >= 7:
        try:
            return int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
        except ValueError:
            return None
    return None


def side_of(obj: dict) -> str:
    """Clasifica por color: azul/verde = aliado, rojo = enemigo."""
    c = parse_color(obj)
    if not c:
        return "neutral"
    r, g, b = c
    if r > 150 and r > g + 60 and r > b + 60:
        return "enemy"
    if b > r + 40 or (g > r + 40 and g > b):
        return "friendly"
    return "neutral"


def bearing(dx: float, dy: float) -> float:
    """Rumbo de brújula de un vector (dx este, dy sur)."""
    return math.degrees(math.atan2(dx, -dy)) % 360.0


def rel_turn(heading: float, target_bearing: float) -> float:
    """Giro necesario en grados: negativo = izquierda, positivo = derecha."""
    return (target_bearing - heading + 540.0) % 360.0 - 180.0


def unit_from_bearing(b: float) -> tuple[float, float]:
    r = math.radians(b)
    return math.sin(r), -math.cos(r)


def num(d: dict, key: str) -> float | None:
    v = d.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return None


class SlopeWindow:
    """Ventana temporal con regresión lineal: derivadas sin el ruido de diferenciar dos puntos."""

    def __init__(self, window_s: float, min_frac: float = 0.4):
        self.window = window_s
        self.min_span = window_s * min_frac
        self.buf: deque[tuple[float, float]] = deque()

    def add(self, t: float, v: float) -> None:
        self.buf.append((t, v))
        while self.buf and t - self.buf[0][0] > self.window:
            self.buf.popleft()

    def reset(self) -> None:
        self.buf.clear()

    def span(self) -> float:
        return self.buf[-1][0] - self.buf[0][0] if len(self.buf) > 1 else 0.0

    def slope(self) -> float | None:
        n = len(self.buf)
        if n < 3 or self.span() < self.min_span:
            return None
        mt = sum(t for t, _ in self.buf) / n
        mv = sum(v for _, v in self.buf) / n
        den = sum((t - mt) ** 2 for t, _ in self.buf)
        if den <= 0:
            return None
        return sum((t - mt) * (v - mv) for t, v in self.buf) / den

    def mean(self) -> float | None:
        if not self.buf:
            return None
        return sum(v for _, v in self.buf) / len(self.buf)


class AngleUnwrap:
    def __init__(self):
        self.prev: float | None = None
        self.acc = 0.0

    def __call__(self, a: float) -> float:
        if self.prev is None:
            self.acc = a
        else:
            self.acc += (a - self.prev + 540.0) % 360.0 - 180.0
        self.prev = a
        return self.acc

    def reset(self):
        self.prev = None


def split_trail(trail) -> list[list]:
    """Trocea el rastro en tramos (uno por vida) separados por None, ignorando tramos vacíos.

    La usan tanto el mapa (dibujar / detectar clic) como el modelo (borrar): si cada uno
    troceara a su manera, los índices no coincidirían y se borraría el tramo equivocado.
    """
    segments, current = [], []
    for pt in trail:
        if pt is None:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(pt)
    if current:
        segments.append(current)
    return segments


# --------------------------------------------------------------------------- modelos

@dataclass
class Airfield:
    key: str
    ax: float
    ay: float
    bx: float
    by: float
    side: str
    color: str

    @property
    def cx(self):
        return (self.ax + self.bx) / 2

    @property
    def cy(self):
        return (self.ay + self.by) / 2

    @property
    def length(self):
        return math.hypot(self.bx - self.ax, self.by - self.ay)


@dataclass
class Target:
    name: str
    x: float
    y: float
    dist: float
    brg: float
    eta: float | None
    side: str


@dataclass
class Snapshot:
    in_game: bool = False
    has_player: bool = False
    aircraft: str = ""
    map_size: tuple[float, float] = (0.0, 0.0)
    px: float = 0.0
    py: float = 0.0
    heading: float | None = None
    tas: float | None = None          # m/s
    ias: float | None = None          # m/s
    alt: float | None = None          # m
    vy: float | None = None           # m/s
    # navegación
    gs: float | None = None           # velocidad sobre el suelo, m/s
    track: float | None = None
    wind_speed: float | None = None   # m/s
    wind_from: float | None = None
    home: Airfield | None = None
    home_auto: bool = True
    home_dist: float | None = None
    home_brg: float | None = None
    home_turn: float | None = None
    home_hdg_corr: float | None = None
    home_eta: float | None = None
    # aproximación
    rwy_hdg: float | None = None
    thr_x: float = 0.0
    thr_y: float = 0.0
    app_along: float | None = None    # m antes del umbral (positivo = en el lado de aproximación)
    app_lateral: float | None = None  # m, positivo = a la derecha del eje
    app_target_agl: float | None = None
    app_dev: float | None = None      # + alto, - bajo
    field_elev: float | None = None
    field_elev_known: bool = False
    entry_x: float = 0.0
    entry_y: float = 0.0
    entry_dist: float | None = None
    entry_brg: float | None = None
    # combustible
    fuel: float | None = None
    fuel_max: float | None = None
    burn: float | None = None         # kg/s
    endurance: float | None = None    # s
    range_m: float | None = None
    fuel_home: float | None = None
    reserve_kg: float | None = None
    bingo_margin_s: float | None = None
    fuel_status: str = ""
    # energía
    es: float | None = None
    ps: float | None = None
    turn_rate: float | None = None    # deg/s
    turn_radius: float | None = None
    turn_360: float | None = None
    glide_ratio: float | None = None
    glide_live: bool = False
    glide_reach: float | None = None
    glide_home_ok: bool | None = None
    glide_speed: float | None = None  # m/s a la que se midió el planeo (depende mucho de ella)
    glide_cx: float = 0.0             # centro del anillo de planeo, desplazado por el viento
    glide_cy: float = 0.0
    # motores
    engines: list = field(default_factory=list)
    wep_now: float = 0.0
    wep_total: float = 0.0
    is_jet: bool = False              # reactor: el juego da empuje (thrust) en vez de potencia
    fuel_leak: float | None = None    # kg/s perdidos con el motor parado o al ralentí
    # objetivo marcado
    wp: tuple[float, float] | None = None
    wp_dist: float | None = None
    wp_brg: float | None = None
    wp_turn: float | None = None
    wp_hdg_corr: float | None = None
    wp_eta: float | None = None
    bomb_release: float | None = None
    bomb_time: float | None = None
    bomb_fall: float | None = None
    # listas
    targets: list = field(default_factory=list)
    threat: Target | None = None
    aa_threat: Target | None = None   # antiaéreo enemigo (SAM/SPAA) más cercano
    airfields: list = field(default_factory=list)
    # sesión
    flight_time: float = 0.0
    flown_m: float = 0.0
    max_g: float = 0.0
    trail: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    tracks: list = field(default_factory=list)   # aviones enemigos seguidos (tracker.Track)
    now: float = 0.0                              # instante del snapshot (para conos y memoria)


@dataclass
class EngineInfo:
    idx: int
    water: float | None
    oil: float | None
    water_rate: float | None   # °C/min
    oil_rate: float | None
    water_ttl: float | None    # s hasta el límite
    oil_ttl: float | None


# --------------------------------------------------------------------------- motor

class FlightModel:
    def __init__(self):
        self.limits: dict[str, dict] = {}   # aircraft type -> {"water": x, "oil": y}
        self.reserve_min = 3.0
        self.home_key: str | None = None
        self.burn_by_regime: dict = {}          # (avión, régimen) -> kg/s aprendidos
        self._af_reg: list = []                 # aeródromos conocidos con identidad estable
        self._af_next = 1
        self.waypoint: tuple[float, float] | None = None   # normalizado
        self.field_elev: dict[str, float] = {}
        self.glide_by_type: dict[str, float] = {}
        self._generation = None
        from .tracker import EnemyTracker          # import local: tracker importa de calc
        self.tracker = EnemyTracker()
        self._reset_all()

    # -- reset
    def _reset_flight(self):
        self.w_px = SlopeWindow(4.0)
        self.w_py = SlopeWindow(4.0)
        self.w_es = SlopeWindow(2.5)
        self.w_hdg = SlopeWindow(1.5)
        self.unwrap = AngleUnwrap()
        self.w_fuel = SlopeWindow(30.0, min_frac=0.2)   # estimación válida desde 6 s
        self._thrust_hist = deque()
        self._fuel_regime = None
        self.w_wind_x = SlopeWindow(25.0)
        self.w_wind_y = SlopeWindow(25.0)
        self.w_glide = SlopeWindow(6.0)
        self.w_fuel_short = SlopeWindow(4.0)
        self.w_tas = SlopeWindow(4.0)
        self.w_temp: dict[tuple[int, str], SlopeWindow] = {}
        self.last_pos = None
        self.last_fuel = None
        self.thr_lock = None
        self.wep_now = 0.0

    def _reset_all(self):
        self._reset_flight()
        self.trail: deque = deque(maxlen=1500)
        self.flight_time = 0.0
        self.flown_m = 0.0
        self.max_g = 0.0
        self.wep_total = 0.0
        self.last_t = None

    def new_match(self):
        self.home_key = None
        self.waypoint = None
        self._af_reg = []
        self.field_elev.clear()
        self.tracker.reset()
        self._reset_all()

    def remove_trail_segment(self, idx: int):
        """Elimina el segmento de rastro con índice idx (0 = más antiguo)."""
        segments = split_trail(self.trail)
        if 0 <= idx < len(segments):
            segments.pop(idx)
        self.trail.clear()
        for i, seg in enumerate(segments):
            for pt in seg:
                self.trail.append(pt)
            if i < len(segments) - 1:
                self.trail.append(None)

    # -- api pública
    def update(self, now: float, state: dict, ind: dict, objs: list, info: dict) -> Snapshot:
        s = Snapshot()
        if not info or not info.get("valid", True) or "map_min" not in info:
            return s
        gen = info.get("map_generation")
        if gen != self._generation:
            self._generation = gen
            self.new_match()

        mn, mx = info["map_min"], info["map_max"]
        sx, sy = float(mx[0] - mn[0]), float(mx[1] - mn[1])
        s.in_game = True
        s.map_size = (sx, sy)
        dt = 0.0 if self.last_t is None else max(0.0, min(now - self.last_t, 2.0))
        self.last_t = now

        objs = objs if isinstance(objs, list) else []
        state = state if state and state.get("valid") else {}
        ind = ind if ind and ind.get("valid") else {}
        s.aircraft = str(ind.get("type", "") or "")

        # ---- objetos del mapa
        player = None
        for o in objs:
            if o.get("type") == "airfield" and "sx" in o:
                af = Airfield(
                    key="",
                    ax=o["sx"] * sx, ay=o["sy"] * sy, bx=o["ex"] * sx, by=o["ey"] * sy,
                    side=side_of(o), color=o.get("color", "#888"),
                )
                s.airfields.append(af)
            elif o.get("icon") == "Player":
                player = o
        self._assign_airfield_keys(s.airfields)

        # ---- datos de vuelo
        tas_kmh, ias_kmh = num(state, "TAS, km/h"), num(state, "IAS, km/h")
        s.tas = tas_kmh / 3.6 if tas_kmh is not None else None
        s.ias = ias_kmh / 3.6 if ias_kmh is not None else None
        s.alt = num(state, "H, m")
        s.vy = num(state, "Vy, m/s")
        ny = num(state, "Ny")

        # Seguimiento de enemigos: también sin jugador (muerto / esperando), para no perder contactos
        s.now = now
        s.tracks = self.tracker.update(now, objs, sx, sy)

        if player is None:
            s.trail = list(self.trail)
            self._fill_targets(s, objs, sx, sy, None, None)
            return s

        s.has_player = True
        px, py = player["x"] * sx, player["y"] * sy
        s.px, s.py = px, py
        if "dx" in player and "dy" in player and (player["dx"] or player["dy"]):
            s.heading = bearing(player["dx"], player["dy"])

        # respawn / salto de posición -> reiniciar estimadores
        if self.last_pos is not None:
            jump = math.hypot(px - self.last_pos[0], py - self.last_pos[1])
            if jump > 3000 or (dt > 0 and jump / dt > 1500):
                self._reset_flight()
                self.trail.append(None)
            else:
                airborne = s.ias is not None and s.ias > 25
                if airborne:
                    self.flown_m += jump
        self.last_pos = (px, py)

        airborne = s.ias is not None and s.ias > 25
        if airborne:
            self.flight_time += dt
        if ny is not None and airborne:
            self.max_g = max(self.max_g, ny)
        if not self.trail or self.trail[-1] is None or \
                math.hypot(px - self.trail[-1][0], py - self.trail[-1][1]) > 150:
            self.trail.append((px, py))
        s.trail = list(self.trail)

        # ---- velocidad sobre el suelo
        self.w_px.add(now, px)
        self.w_py.add(now, py)
        vx, vyg = self.w_px.slope(), self.w_py.slope()
        if vx is not None and vyg is not None:
            s.gs = math.hypot(vx, vyg)
            if s.gs > 5:
                s.track = bearing(vx, vyg)

        # ---- giro
        if s.heading is not None:
            self.w_hdg.add(now, self.unwrap(s.heading))
            tr = self.w_hdg.slope()
            if tr is not None:
                s.turn_rate = tr
                v = s.tas or s.gs
                if abs(tr) > 1.0 and v:
                    s.turn_radius = v / math.radians(abs(tr))
                    s.turn_360 = 360.0 / abs(tr)

        # ---- viento: solo en vuelo recto y a velocidad estable. La ventana de 4 s de la
        # velocidad sobre el suelo va retrasada; si aceleras o giras, ese retraso parecería viento.
        if s.tas is not None:
            self.w_tas.add(now, s.tas)
        acc = self.w_tas.slope()
        steady = (acc is not None and abs(acc) < 0.6 and s.turn_rate is not None and abs(s.turn_rate) < 1.0
                  and self.w_hdg.span() > 1.0)
        if steady and s.heading is not None and s.tas and vx is not None and s.gs and s.gs > 30:
            vh = math.sqrt(max(s.tas ** 2 - (s.vy or 0.0) ** 2, 0.0))
            ux, uy = unit_from_bearing(s.heading)
            self.w_wind_x.add(now, vx - vh * ux)
            self.w_wind_y.add(now, vyg - vh * uy)
        wx, wy = self.w_wind_x.mean(), self.w_wind_y.mean()
        if wx is not None and self.w_wind_x.span() > 5:
            s.wind_speed = math.hypot(wx, wy)
            s.wind_from = bearing(-wx, -wy)
            wind = (wx, wy) if s.wind_speed > 1.5 else (0.0, 0.0)
        else:
            wind = (0.0, 0.0)

        # ---- energía
        if s.alt is not None and s.tas is not None:
            s.es = s.alt + s.tas ** 2 / (2 * G)
            self.w_es.add(now, s.es)
            s.ps = self.w_es.slope()

        # ---- aeródromos: elevación aprendida y base
        for af in s.airfields:
            if (s.ias is not None and s.ias < 12 and s.vy is not None and abs(s.vy) < 0.5
                    and s.alt is not None and math.hypot(px - af.cx, py - af.cy) < max(2500, af.length)):
                self.field_elev[af.key] = s.alt
        home = self._pick_home(s.airfields, px, py)
        s.home = home
        s.home_auto = self.home_key is None
        speed_for_eta = s.tas or s.gs

        def nav_to(tx, ty):
            d = math.hypot(tx - px, ty - py)
            b = bearing(tx - px, ty - py)
            turn = rel_turn(s.heading, b) if s.heading is not None else None
            hdg, gs_c = self._wind_correct(b, s.tas, wind)
            if gs_c is None:
                gs_c = speed_for_eta
            eta = d / gs_c if gs_c and gs_c > 5 else None
            return d, b, turn, hdg, eta, gs_c

        if home:
            s.home_dist, s.home_brg, s.home_turn, s.home_hdg_corr, s.home_eta, _ = nav_to(home.cx, home.cy)
            s.field_elev_known = home.key in self.field_elev
            s.field_elev = self.field_elev.get(home.key, 0.0)
            self._approach(s, home, px, py)

        # ---- combustible
        fuel = num(state, "Mfuel, kg")
        s.fuel, s.fuel_max = fuel, num(state, "Mfuel0, kg")
        # Consumo por RÉGIMEN de motor: la postcombustión gasta ~5 veces más que ir a fondo sin
        # ella (Su-30 real: 13,7 frente a 2,7 kg/s). Promediar 30 s mezclando regímenes daba
        # cifras intermedias durante medio minuto. Ahora la ventana se reinicia al cambiar de
        # régimen y, mientras no hay datos nuevos, se usa lo aprendido para ese régimen.
        thr_vals = [v for v in (num(state, f"throttle {i}, %") for i in range(1, 5)) if v is not None]
        thr_max = max(thr_vals) if thr_vals else None
        thrust_vals = [v for v in (num(state, f"thrust {i}, kgs") for i in range(1, 5)) if v is not None]
        if thrust_vals:
            self._thrust_hist.append((now, sum(thrust_vals)))
            while self._thrust_hist and now - self._thrust_hist[0][0] > 2.0:
                self._thrust_hist.popleft()
        if thr_max is None:
            regime = None
        elif thr_max > 100.5:
            regime = "postcombustion"
        elif thr_max <= 5:
            regime = "ralenti"
        else:
            regime = f"{int(round(thr_max / 10.0)) * 10}%"
        if fuel is not None:
            if self.last_fuel is not None and fuel > self.last_fuel + 1.0:
                self.w_fuel.reset()   # repostaje
            if regime != self._fuel_regime:
                self.w_fuel.reset()
                self._fuel_regime = regime
                self._regime_t0 = now
            self.last_fuel = fuel
            # Tras cambiar de régimen el motor tarda en subir o bajar de vueltas (en tu Su-30 la
            # postcombustión tardó hasta 6 s en dar empuje pleno) y ese consumo de transición no
            # es el del régimen. Se espera 3 s y, si el avión da empuje, a que se estabilice
            # (menos de un 5 % de variación en 2 s).
            settled = now - getattr(self, "_regime_t0", now) >= 3.0
            if settled and thrust_vals and len(self._thrust_hist) >= 2:
                vals = [v for _, v in self._thrust_hist]
                settled = max(vals) - min(vals) <= 0.05 * max(max(vals), 1.0)
            if settled:
                self.w_fuel.add(now, fuel)
            elif len(self.w_fuel.buf):
                self.w_fuel.reset()             # solo cuenta tramo estable y continuo
            sl = self.w_fuel.slope()
            rkey = (s.aircraft, regime)
            if sl is not None and sl < -1e-4:
                self.burn_by_regime[rkey] = -sl
            burn = self.burn_by_regime.get(rkey)
            if burn:
                s.burn = burn
                s.endurance = fuel / s.burn
                if speed_for_eta:
                    s.range_m = s.endurance * (s.gs or speed_for_eta)
                s.reserve_kg = s.burn * self.reserve_min * 60
                if s.home_eta is not None:
                    s.fuel_home = s.home_eta * s.burn
                    s.bingo_margin_s = (fuel - s.fuel_home - s.reserve_kg) / s.burn
                    if s.bingo_margin_s < 0:
                        s.fuel_status = "BINGO"
                    elif s.bingo_margin_s < 120:
                        s.fuel_status = "JUSTO"
                    else:
                        s.fuel_status = "OK"

        # ---- motores, WEP y planeo
        engines, total_power, throttles = [], 0.0, []
        total_thrust, has_thrust, has_power = 0.0, False, False
        lim = self.limits.get(s.aircraft, {})
        for i in range(1, 5):
            w, o = num(state, f"water temp {i}, C"), num(state, f"oil temp {i}, C")
            thr = num(state, f"throttle {i}, %")
            p = num(state, f"power {i}, hp")
            th = num(state, f"thrust {i}, kgs")
            if w is None and o is None and thr is None:
                continue
            if thr is not None:
                throttles.append(thr)
            if p is not None:
                total_power += p
                has_power = True
            if th is not None:
                total_thrust += th
                has_thrust = True
            rates, ttls = {}, {}
            for kind, val in (("water", w), ("oil", o)):
                rates[kind] = ttls[kind] = None
                if val is None:
                    continue
                win = self.w_temp.setdefault((i, kind), SlopeWindow(20.0))
                win.add(now, val)
                r = win.slope()
                if r is not None:
                    rates[kind] = r * 60.0
                    limit = lim.get(kind)
                    if limit and r > 0.01 and val < limit:
                        ttls[kind] = (limit - val) / r
                    elif limit and val >= limit:
                        ttls[kind] = 0.0
            engines.append(EngineInfo(i, w, o, rates["water"], rates["oil"], ttls["water"], ttls["oil"]))
        s.engines = engines

        if throttles and max(throttles) > 100.5:
            self.wep_now += dt
            self.wep_total += dt
        else:
            self.wep_now = 0.0
        s.wep_now, s.wep_total = self.wep_now, self.wep_total

        # ¿Motor sin empujar? En reactores el juego da "power" siempre a 0 y la información
        # real está en "thrust": usar la potencia ahí haría creer que vas sin motor siempre.
        s.is_jet = has_thrust
        idle = bool(throttles) and max(throttles) <= 5.0
        if has_thrust:
            idle = idle or total_thrust < 150.0
        elif has_power:
            idle = idle or total_power < 30.0

        # Fuga de combustible: con el motor parado o al ralentí el consumo es casi nulo
        # (un Su-30 al ralentí gasta ~0,1 kg/s); si el depósito baja deprisa, se está vaciando.
        # La ventana solo cuenta tramos con el motor ya parado: si no, al cortar gas arrastraría
        # el consumo de la postcombustión de los segundos anteriores y daría una fuga falsa.
        if fuel is not None and idle:
            self.w_fuel_short.add(now, fuel)
            fs = self.w_fuel_short.slope()
            if fs is not None and fs < -1.0 and self.w_fuel_short.span() >= 3.0:
                s.fuel_leak = -fs
        else:
            self.w_fuel_short.reset()

        # Planeo por ENERGÍA, no por velocidad vertical: al cortar motor a alta velocidad el
        # avión pierde sobre todo velocidad, no altura, y Vy sola daría un planeo irreal
        # (en tu Su-30 a 830 km/h: 1:68 por Vy, frente a 1:5 real). La pérdida de energía
        # total (Ps) incluye las dos cosas. Con aerofrenos o tren fuera no se mide.
        # Solo cuenta vuelo cercano a 1 G: tirando de la palanca (subida a zoom, giros) la
        # resistencia se dispara y el planeo saldría mucho peor de lo que es.
        dirty = (num(state, "airbrake, %") or 0) > 0 or (num(state, "gear, %") or 0) > 0
        ny_ok = ny is None or abs(ny - 1.0) < 0.25
        if idle and airborne and not dirty and ny_ok and s.ps is not None and s.ps < -0.5 and s.tas:
            vh = math.sqrt(max(s.tas ** 2 - (s.vy or 0.0) ** 2, 0.0))
            ratio = vh / -s.ps
            if 2.0 <= ratio <= 40.0:            # fuera de esto es un choque, un derribo o ruido
                self.w_glide.add(now, ratio)
                self._glide_speed = s.tas
            gm = self.w_glide.mean()
            if gm and self.w_glide.span() > 3:
                self.glide_by_type[s.aircraft] = gm
                s.glide_live = True
        else:
            self.w_glide.reset()
        s.glide_ratio = self.glide_by_type.get(s.aircraft)
        s.glide_speed = getattr(self, "_glide_speed", None) if s.glide_ratio else None
        if s.glide_ratio and s.alt is not None:
            # Energía disponible = altura sobre la pista + la velocidad que te sobra por encima de
            # la de aterrizaje (300 km/h): a 820 km/h un reactor lleva ~2,6 km de "altura" en
            # forma de velocidad, que puede convertir en distancia planeando.
            agl = max(0.0, s.alt - (s.field_elev or 0.0))
            v_land = 300 / 3.6
            spare = max(0.0, (s.tas or 0.0) ** 2 - v_land ** 2) / (2 * G)
            s.glide_reach = (agl + spare) * s.glide_ratio     # en aire en calma
            s.glide_cx, s.glide_cy = px, py
            vh = s.tas or 0.0
            if vh > 20:
                t_air = s.glide_reach / vh                     # segundos aprox. planeando
                s.glide_cx, s.glide_cy = px + wind[0] * t_air, py + wind[1] * t_air
                if s.home and s.home_dist is not None:
                    _, gs_home = self._wind_correct(s.home_brg, vh, wind)
                    if gs_home and gs_home > 0:
                        s.glide_home_ok = t_air * gs_home * 0.85 > s.home_dist
                    else:
                        s.glide_home_ok = False

        # ---- objetivo marcado
        if self.waypoint:
            wxm, wym = self.waypoint[0] * sx, self.waypoint[1] * sy
            s.wp = (wxm, wym)
            s.wp_dist, s.wp_brg, s.wp_turn, s.wp_hdg_corr, s.wp_eta, gs_c = nav_to(wxm, wym)
            if s.alt is not None and gs_c:
                h = s.alt - (s.field_elev or 0.0)
                if h > 20:
                    s.bomb_fall = math.sqrt(2 * h / G)
                    s.bomb_release = gs_c * s.bomb_fall
                    s.bomb_time = (s.wp_dist - s.bomb_release) / gs_c

        self._fill_targets(s, objs, sx, sy, px, py, nav_to)
        s.flight_time, s.flown_m, s.max_g = self.flight_time, self.flown_m, self.max_g
        s.alerts = self._alerts(s)
        return s

    # -- piezas
    def _assign_airfield_keys(self, airfields):
        """Identidad estable por aeródromo, aunque se mueva (portaaviones).

        Antes la identidad era la posición redondeada: un portaaviones cambiaba de identidad
        cada pocos segundos y, si lo habías elegido como base, la app lo perdía. Ahora cada
        aeródromo se empareja con el conocido más cercano del mismo bando y longitud parecida.
        """
        used = set()
        for af in sorted(airfields, key=lambda a: -a.length):
            best, bd = None, 1500.0
            for r in self._af_reg:
                if r["id"] in used or r["side"] != af.side:
                    continue
                if not (0.6 < af.length / max(r["length"], 1.0) < 1.6):
                    continue
                d = math.hypot(af.cx - r["cx"], af.cy - r["cy"])
                if d < bd:
                    best, bd = r, d
            if best is None:
                best = {"id": f"af{self._af_next}", "side": af.side}
                self._af_next += 1
                self._af_reg.append(best)
            best.update(cx=af.cx, cy=af.cy, length=af.length)
            used.add(best["id"])
            af.key = best["id"]

    def _pick_home(self, airfields, px, py):
        if not airfields:
            return None
        if self.home_key:
            for af in airfields:
                if af.key == self.home_key:
                    return af
        friendly = [a for a in airfields if a.side == "friendly"] or airfields
        # Las pistas cortas (portaaviones ~300 m) no sirven de base automática para cualquier
        # avión; siguen disponibles eligiéndolas a mano con doble clic.
        candidates = [a for a in friendly if a.length >= MIN_HOME_RUNWAY_M] or friendly
        return min(candidates, key=lambda a: math.hypot(a.cx - px, a.cy - py))

    @staticmethod
    def _wind_correct(course, tas, wind):
        """Rumbo a volar y velocidad sobre el suelo para mantener `course` con viento."""
        if not tas or tas < 20:
            return None, None
        wx, wy = wind
        cx, cy = unit_from_bearing(course)
        rx, ry = -cy, cx                       # derecha del rumbo
        cross = wx * rx + wy * ry
        head = wx * cx + wy * cy
        ratio = max(-0.95, min(0.95, cross / tas))
        wca = -math.degrees(math.asin(ratio))
        gs = tas * math.cos(math.radians(wca)) + head
        return (course + wca) % 360.0, gs

    def _approach(self, s: Snapshot, af: Airfield, px, py):
        if af.length < 50:
            return
        # Umbral = extremo de pista más cercano. Se fija solo mientras estás en final cerca
        # (lado de aproximación, < 6 km) para que no salte al cruzar la mitad de la pista.
        dist_c = math.hypot(px - af.cx, py - af.cy)
        end = None
        if self.thr_lock and self.thr_lock[0] == af.key and dist_c < 6000:
            e = self.thr_lock[1]
            tx, ty, ox, oy = (af.ax, af.ay, af.bx, af.by) if e == "a" else (af.bx, af.by, af.ax, af.ay)
            if (px - tx) * (ox - tx) + (py - ty) * (oy - ty) < 0:     # sigues antes del umbral
                end = e
        if end is None:
            da = math.hypot(px - af.ax, py - af.ay)
            db = math.hypot(px - af.bx, py - af.by)
            end = "a" if da <= db else "b"
            self.thr_lock = (af.key, end)
        if end == "a":
            tx, ty, ox, oy = af.ax, af.ay, af.bx, af.by
        else:
            tx, ty, ox, oy = af.bx, af.by, af.ax, af.ay
        L = af.length
        ux, uy = (ox - tx) / L, (oy - ty) / L          # dirección de aterrizaje
        rx, ry = -uy, ux                               # derecha del eje
        vx, vy = px - tx, py - ty
        s.rwy_hdg = bearing(ux, uy)
        s.thr_x, s.thr_y = tx, ty
        s.app_along = -(vx * ux + vy * uy)
        s.app_lateral = vx * rx + vy * ry
        s.entry_x, s.entry_y = tx - ux * ENTRY_POINT_M, ty - uy * ENTRY_POINT_M
        s.entry_dist = math.hypot(s.entry_x - px, s.entry_y - py)
        s.entry_brg = bearing(s.entry_x - px, s.entry_y - py)
        if s.app_along > 0 and s.alt is not None:
            s.app_target_agl = s.app_along * math.tan(math.radians(GLIDE_SLOPE_DEG)) + THRESHOLD_CROSS_M
            s.app_dev = (s.alt - (s.field_elev or 0.0)) - s.app_target_agl

    def _fill_targets(self, s, objs, sx, sy, px, py, nav_to=None):
        out, threat, aa = [], None, None
        for o in objs:
            if o.get("type") == "airfield" or o.get("icon") == "Player" or "x" not in o:
                continue
            side = side_of(o)
            x, y = o["x"] * sx, o["y"] * sy
            if px is None:
                continue
            d, b, _, _, eta, _ = nav_to(x, y)
            icon = o.get("icon", "?")
            if icon in TARGET_ICONS:
                out.append(Target(icon, x, y, d, b, eta, side))
            elif icon in AA_ICONS:
                if side == "enemy" and (aa is None or d < aa.dist):
                    aa = Target(icon, x, y, d, b, eta, side)
            elif side == "enemy" and any(w in icon for w in SHIP_WORDS):
                out.append(Target(icon, x, y, d, b, eta, side))
            elif side == "enemy" and o.get("type") == "aircraft":
                if threat is None or d < threat.dist:
                    threat = Target(icon, x, y, d, b, eta, side)
        out.sort(key=lambda t: t.dist)
        s.targets = out[:6]
        s.threat = threat
        s.aa_threat = aa

    def _alerts(self, s: Snapshot):
        a = []
        if s.fuel_leak:
            a.append(("crit", f"Posible fuga de combustible: {s.fuel_leak:.0f} kg/s con el motor parado"))
        if s.fuel_status == "BINGO":
            a.append(("crit", "BINGO FUEL: vuelve ya a base"))
        elif s.fuel_status == "JUSTO":
            a.append(("warn", f"Combustible justo: margen {fmt_time(s.bingo_margin_s)}"))
        for e in s.engines:
            for kind, ttl in (("agua", e.water_ttl), ("aceite", e.oil_ttl)):
                if ttl is not None and ttl < 60:
                    lvl = "crit" if ttl < 15 else "warn"
                    txt = "en límite" if ttl <= 0 else f"en {ttl:.0f} s"
                    a.append((lvl, f"Motor {e.idx}: {kind} {txt}"))
        if s.glide_live and s.glide_home_ok is False:
            a.append(("warn", "Planeando: NO llegas a base"))
        return a


def fmt_time(sec: float | None) -> str:
    if sec is None or sec != sec:
        return "--"
    neg = sec < 0
    sec = abs(int(sec))
    m, s_ = divmod(sec, 60)
    h, m = divmod(m, 60)
    t = f"{h}:{m:02d}:{s_:02d}" if h else f"{m}:{s_:02d}"
    return ("-" if neg else "") + t


def fmt_dist(m: float | None) -> str:
    if m is None:
        return "--"
    return f"{m / 1000:.1f} km" if abs(m) >= 1000 else f"{m:.0f} m"
