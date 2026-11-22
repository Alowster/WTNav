"""Filtros del mapa: qué se muestra y qué se fija al borde de la brújula. Python puro (sin Qt).

Cada objeto del JSON del juego se clasifica por BANDO (enemigo / aliado / neutral) y por
CATEGORÍA (cazas, antiaéreos, buques...), usando los nombres de icono vistos en partidas
reales. Lo que no se reconoce va a "otros" para que nada desaparezca sin que lo sepas.

Un objeto se dibuja si su bando Y su categoría están activados. Las categorías "con borde"
tienen además la opción de fijarse al borde del círculo cuando quedan fuera del radio.

El mapa cuadrado y el circular tienen filtros independientes, guardados por separado.
"""
from __future__ import annotations

import json

from .calc import side_of

# Iconos vistos en partidas reales (y algunas variantes del mismo patrón de nombre)
AIR_FIGHTER = {"Fighter"}
AIR_BOMBER = {"Bomber"}
AIR_ASSAULT = {"Assault"}
AA_ICONS = {"SAM", "SPAA", "Airdefence"}
STRUCT_ICONS = {"Pillbox", "Structure", "Bunker"}
SHIP_WORDS = ("Destroyer", "Frigate", "Boat", "Cruiser", "Battleship", "Ship", "Carrier", "Submarine")
VEHICLE_WORDS = ("Tank", "IFV", "Wheeled", "Artillery", "TankDestroyer", "APC", "Truck", "Car", "Train")
OBJECTIVE_TYPES = {"bombing_point", "capture_zone", "defending_point", "waypoint"}
RESPAWN_TYPES = {"respawn_base_fighter", "respawn_base_bomber", "respawn_base_tank"}
CARRIER_MAX_M = 1000.0   # pistas más cortas: portaaviones (en tus partidas, ~300 m y en movimiento)

# (clave, texto, ¿admite borde?, ¿visible por defecto?, ¿al borde por defecto?)
SIDES = [
    ("enemy", "Enemigos"),
    ("friendly", "Aliados"),
    ("neutral", "Neutrales"),
]
OBJECT_CATS = [
    ("fighter",   "Cazas",                     True,  True,  True),
    ("bomber",    "Bombarderos",               True,  True,  True),
    ("assault",   "Ataque a tierra",           True,  True,  True),
    ("air_other", "Otros aviones/helicópteros", True, True,  False),
    ("vehicle",   "Vehículos terrestres",      True,  True,  False),
    ("aa",        "Antiaéreos (SAM/SPAA)",     True,  True,  False),
    ("structure", "Estructuras y búnkeres",    True,  True,  False),
    ("ship",      "Buques",                    True,  True,  False),
    ("carrier",   "Portaaviones",              True,  True,  False),
    ("airfield",  "Aeródromos",                True,  True,  False),
    ("respawn",   "Puntos de aparición",       False, False, False),
    ("objective", "Objetivos de misión",       True,  True,  False),
    ("other",     "Otros",                     False, True,  False),
]
SPECIAL = [
    # (clave, texto, ¿admite borde?, visible por defecto, borde por defecto)
    ("target",    "Tu blanco fijado",          True,  True,  True),
    ("home",      "Tu base (línea y marca)",   True,  True,  True),
    ("waypoint",  "Punto marcado",             True,  True,  True),
]
HELPERS = [
    # (clave, texto, visible por defecto en cuadrado, en circular)
    ("cones",     "Conos rojos (rumbo enemigo)",     True,  True),
    ("contacts",  "Conos naranja (contacto)",        True,  False),
    ("trail",     "Rastro",                          True,  True),
    ("rings",     "Anillos de autonomía y planeo",   True,  False),
    ("approach",  "Eje de pista y punto de entrada", True,  True),
    ("grid",      "Cuadrícula A-1",                  True,  False),
    ("compass",   "Anillo de brújula (N/E/S/W)",     False, True),
]
# Ayudas que no tienen sentido en cada modo (no se ofrecen en su menú)
HELPERS_ONLY = {"grid": "square", "compass": "circle"}


def classify(o: dict) -> tuple[str, str]:
    """(bando, categoría) de un objeto del map_obj.json."""
    t = o.get("type", "")
    icon = str(o.get("icon", "") or "")
    side = side_of(o)
    if t == "airfield":
        return side, "airfield"          # la distinción portaaviones/pista la hace classify_airfield
    if t in RESPAWN_TYPES:
        return side, "respawn"
    if t in OBJECTIVE_TYPES or icon in OBJECTIVE_TYPES:
        return side, "objective"
    if t == "aircraft":
        if icon in AIR_FIGHTER:
            return side, "fighter"
        if icon in AIR_BOMBER:
            return side, "bomber"
        if icon in AIR_ASSAULT:
            return side, "assault"
        return side, "air_other"
    if icon in AA_ICONS:
        return side, "aa"
    if icon in STRUCT_ICONS:
        return side, "structure"
    if any(w in icon for w in SHIP_WORDS):
        return side, "ship"
    if t == "ground_model" or any(w in icon for w in VEHICLE_WORDS):
        if any(w in icon for w in VEHICLE_WORDS):
            return side, "vehicle"
    return side, "other"


def classify_airfield(af) -> tuple[str, str]:
    """Para los aeródromos ya convertidos a metros (calc.Airfield): portaaviones si la pista es corta."""
    return af.side, ("carrier" if af.length < CARRIER_MAX_M else "airfield")


class MapFilters:
    """Estado de los filtros de un mapa (cuadrado o circular)."""

    def __init__(self, mode: str):
        self.mode = mode                      # "square" | "circle"
        circle = mode == "circle"
        self.sides = {k: True for k, _ in SIDES}
        self.show = {k: v for k, _, _, v, _ in OBJECT_CATS + SPECIAL}
        self.rim = {k: (r if circle else False) for k, _, can, _, r in OBJECT_CATS + SPECIAL if can}
        self.helpers = {k: (c if circle else s) for k, _, s, c in HELPERS}

    # -- consultas
    def object_visible(self, side: str, cat: str) -> bool:
        return self.sides.get(side, True) and self.show.get(cat, True)

    def object_on_rim(self, side: str, cat: str) -> bool:
        return self.object_visible(side, cat) and self.rim.get(cat, False)

    def special(self, key: str) -> bool:
        return self.show.get(key, True)

    def special_rim(self, key: str) -> bool:
        return self.special(key) and self.rim.get(key, False)

    def helper(self, key: str) -> bool:
        only = HELPERS_ONLY.get(key)
        if only and only != self.mode:
            return False
        return self.helpers.get(key, True)

    def signature(self) -> tuple:
        """Huella para saber si hay que repintar."""
        return (tuple(sorted(self.sides.items())), tuple(sorted(self.show.items())),
                tuple(sorted(self.rim.items())), tuple(sorted(self.helpers.items())))

    # -- guardar / cargar (JSON: tolera claves nuevas o desaparecidas entre versiones)
    def to_json(self) -> str:
        return json.dumps({"sides": self.sides, "show": self.show, "rim": self.rim, "helpers": self.helpers})

    def load_json(self, text) -> None:
        try:
            d = json.loads(text) if text else {}
        except (ValueError, TypeError):
            return
        for name in ("sides", "show", "rim", "helpers"):
            cur = getattr(self, name)
            for k, v in (d.get(name) or {}).items():
                if k in cur and isinstance(v, bool):
                    cur[k] = v
