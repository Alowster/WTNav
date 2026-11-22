"""Guarda cada segundo los JSON del juego en archivos legibles, para mirarlos en PyCharm.

Uso (con el juego abierto):
    python tools/guardar_json.py                 # cada 1 s, hasta Ctrl+C / botón de parar
    python tools/guardar_json.py --intervalo 2   # cada 2 s
    python tools/guardar_json.py --solo-ultimo   # no guarda historial, solo los "ultimo_*.json"

Crea en tools/json_juego/:
    ultimo_state.json, ultimo_indicators.json,       <- se sobrescriben cada segundo: ábrelos en
    ultimo_map_obj.json, ultimo_map_info.json           PyCharm y verás cómo cambian en directo
    historial/0001_state.json, 0001_indicators.json  <- una copia numerada por segundo
Solo usa la biblioteca estándar de Python.
"""
import argparse
import json
import os
import time
import urllib.request

HOST = "http://127.0.0.1:8111"
ENDPOINTS = {"state": "/state", "indicators": "/indicators",
             "map_obj": "/map_obj.json", "map_info": "/map_info.json"}


def get(path, timeout=1.0):
    try:
        with urllib.request.urlopen(HOST + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def write(path, data):
    tmp = path + ".tmp"            # escribir y renombrar: PyCharm nunca ve un archivo a medias
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--intervalo", type=float, default=1.0, help="segundos entre guardados")
    ap.add_argument("--solo-ultimo", action="store_true", help="no guardar historial numerado")
    a = ap.parse_args()

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "json_juego")
    hist = os.path.join(base, "historial")
    os.makedirs(hist, exist_ok=True)
    print(f"Guardando en {base}  (para parar: Ctrl+C o el botón de parar de PyCharm)\n")

    n = 0
    try:
        while True:
            t = time.monotonic()
            n += 1
            got = []
            for name, path in ENDPOINTS.items():
                data = get(path)
                if data is None:
                    continue
                got.append(name)
                write(os.path.join(base, f"ultimo_{name}.json"), data)
                if not a.solo_ultimo:
                    write(os.path.join(hist, f"{n:04d}_{name}.json"), data)
            msg = ", ".join(got) if got else "sin respuesta del juego (¿está abierto?)"
            print(f"\r[{n:4d}] guardado: {msg}          ", end="", flush=True)
            time.sleep(max(0.0, a.intervalo - (time.monotonic() - t)))
    except KeyboardInterrupt:
        pass
    print(f"\n\nListo: {n} guardados en {base}")


if __name__ == "__main__":
    main()
