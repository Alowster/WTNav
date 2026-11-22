"""Versión de la app y repositorio de donde salen las actualizaciones.

Para publicar una versión nueva:
  1. Sube VERSION aquí ANTES de compilar (por ejemplo "1.1.2").
  2. Compila el .exe.
  3. En GitHub crea una Release con el tag "v" + VERSION (v1.1.2) y adjunta el .exe.
Si el .exe lleva una VERSION más baja que su Release, la app se ofrecerá actualizar sin parar.
"""
VERSION = "1.0.1"
REPO = "Alowster/WTNav"
EXE_ASSET = "WTNav.exe"
