"""Graba /state e /indicators del juego mientras juegas, para analizarlos después.

Uso (desde PyCharm o la terminal, con el juego abierto):
    python tools/grabar_8111.py                # graba hasta que pulses Ctrl+C (o pares en PyCharm)
    python tools/grabar_8111.py --segundos 180 # graba 3 minutos y para sola
    python tools/grabar_8111.py --mapa         # incluye también map_obj.json (archivo más grande)

Crea tools/grabaciones/grabacion_AAAAMMDD_HHMMSS.jsonl: una línea JSON por muestra.
Solo usa la biblioteca estándar de Python.
"""
import argparse
import json
import os
import time
import urllib.request
from datetime import datetime

HOST = "http://127.0.0.1:8111"


def get(path, timeout=1.0):
    try:
        with urllib.request.urlopen(HOST + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--segundos", type=float, default=0, help="duración (0 = hasta Ctrl+C)")
    ap.add_argument("--intervalo", type=float, default=0.5, help="segundos entre muestras")
    ap.add_argument("--mapa", action="store_true", help="grabar también map_obj.json")
    a = ap.parse_args()

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grabaciones")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, datetime.now().strftime("grabacion_%Y%m%d_%H%M%S.jsonl"))
    print(f"Grabando en {path}")
    print("Vuela normal: un rato con el acelerador a fondo (postcombustión) y otro con el acelerador al mínimo.")
    print("Para parar: Ctrl+C (o el botón de parar de PyCharm).\n")

    t0 = time.monotonic()
    n = 0
    try:
        with open(path, "w", encoding="utf-8") as f:
            while True:
                t = time.monotonic() - t0
                if a.segundos and t > a.segundos:
                    break
                rec = {"t": round(t, 2), "state": get("/state"), "indicators": get("/indicators")}
                if a.mapa:
                    rec["map_obj"] = get("/map_obj.json")
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                n += 1
                st = rec["state"] or {}
                if not st:
                    msg = "sin respuesta del juego (¿está abierto?)"
                elif not st.get("valid"):
                    msg = "juego abierto, pero no estás en vuelo"
                else:
                    thr = st.get("throttle 1, %", "?")
                    msg = f"acelerador {thr} %  ·  {st.get('IAS, km/h', '?')} km/h  ·  {st.get('H, m', '?')} m"
                print(f"\r[{t:6.1f} s] {n} muestras · {msg}          ", end="", flush=True)
                time.sleep(max(0.0, a.intervalo - ((time.monotonic() - t0) - t)))
    except KeyboardInterrupt:
        pass
    print(f"\n\nListo: {n} muestras en {path}")


if __name__ == "__main__":
    main()
