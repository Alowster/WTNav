"""Actualizaciones desde las Releases de GitHub.

Flujo:
  1. UpdateChecker pregunta a la API de GitHub por la última Release (sin credenciales: el
     repositorio es público; el límite es de 60 consultas por hora e IP, de sobra).
  2. Si su versión es mayor que la nuestra, UpdateDialog la enseña con sus novedades.
  3. Al aceptar se descarga WTNav.exe junto al ejecutable actual y se comprueba (tamaño, SHA-256
     que publica GitHub y que sea un ejecutable de Windows).
  4. Windows no deja sobrescribir un .exe en marcha, pero sí renombrarlo: el actual pasa a
     WTNav.exe.old, el nuevo ocupa su sitio, se guarda el estado, se lanza el nuevo y esta
     instancia se cierra. El .old se borra en el siguiente arranque.

Ejecutando desde el código fuente no se sustituye nada: solo se avisa y se abre la Release.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QTextBrowser,
                               QVBoxLayout)

from .version import EXE_ASSET, REPO, VERSION

API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
TIMEOUT = 15


# ---------------------------------------------------------------------------- utilidades

def parse_version(v: str) -> tuple:
    """'v1.10.2' -> (1, 10, 2). Lo que no sea número se ignora ('1.2.0-beta' -> (1, 2, 0))."""
    nums = re.findall(r"\d+", (v or "").split("-")[0].split("+")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def is_newer(remote: str, local: str = VERSION) -> bool:
    a, b = parse_version(remote), parse_version(local)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def is_frozen() -> bool:
    """True si corre como WTNav.exe compilado con PyInstaller."""
    return bool(getattr(sys, "frozen", False)) and sys.executable.lower().endswith(".exe")


def exe_path() -> Path:
    return Path(sys.executable).resolve()


def cleanup_old():
    """Borra el ejecutable antiguo que dejó la última actualización (si ya no está en uso)."""
    if not is_frozen():
        return
    for leftover in (exe_path().with_name(exe_path().name + ".old"),
                     exe_path().with_name(EXE_ASSET + ".download")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass            # aún bloqueado: se intentará la próxima vez


def _request(url: str, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={
        "Accept": accept,
        "User-Agent": f"WTNav/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def _friendly_error(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return "No hay ninguna versión publicada todavía."
        if e.code == 403:
            return "GitHub ha limitado las consultas. Prueba dentro de un rato."
        return f"GitHub respondió con error {e.code}."
    if isinstance(e, urllib.error.URLError):
        return "No se pudo conectar con GitHub. ¿Hay conexión a internet?"
    if isinstance(e, TimeoutError):
        return "GitHub tardó demasiado en responder."
    return f"Error inesperado: {e}"


# ---------------------------------------------------------------------------- comprobación

class UpdateChecker(QObject):
    """Consulta la última Release en segundo plano. Emite una sola de las tres señales."""
    found = Signal(dict)        # {"version", "notes", "url", "page", "size", "sha256"}
    upToDate = Signal(str)      # última versión publicada
    failed = Signal(str)

    def check(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            with _request(API_LATEST) as r:
                data = json.load(r)
        except Exception as e:                      # noqa: BLE001 - se informa al usuario
            self.failed.emit(_friendly_error(e))
            return
        tag = data.get("tag_name") or ""
        if not is_newer(tag):
            self.upToDate.emit(tag.lstrip("vV") or VERSION)
            return
        asset = next((a for a in data.get("assets", [])
                      if a.get("name", "").lower() == EXE_ASSET.lower()), None)
        digest = (asset or {}).get("digest") or ""
        self.found.emit({
            "version": tag.lstrip("vV"),
            "notes": data.get("body") or "",
            "page": data.get("html_url") or RELEASES_PAGE,
            "url": (asset or {}).get("browser_download_url"),
            "size": (asset or {}).get("size") or 0,
            "sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
        })


# ---------------------------------------------------------------------------- descarga e instalación

class Downloader(QObject):
    progress = Signal(int, int)      # bytes recibidos, total
    finished = Signal(str)           # ruta del exe descargado y comprobado
    failed = Signal(str)

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = info
        self._cancel = False

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self._cancel = True

    def _run(self):
        # Se descarga en la misma carpeta que el exe: así el cambio final es un simple renombrado
        dest = exe_path().with_name(EXE_ASSET + ".download")
        try:
            h = hashlib.sha256()
            got = 0
            with _request(self.info["url"], accept="application/octet-stream") as r, open(dest, "wb") as f:
                total = int(r.headers.get("Content-Length") or self.info.get("size") or 0)
                while True:
                    if self._cancel:
                        raise InterruptedError
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self.progress.emit(got, total)
            problem = self._verify(dest, got, h.hexdigest())
            if problem:
                raise ValueError(problem)
        except InterruptedError:
            dest.unlink(missing_ok=True)
            self.failed.emit("")
            return
        except PermissionError:
            self.failed.emit("No hay permiso para escribir en la carpeta de la app. "
                             "Muévela fuera de 'Archivos de programa' (por ejemplo a Documentos).")
            return
        except ValueError as e:
            dest.unlink(missing_ok=True)
            self.failed.emit(str(e))
            return
        except Exception as e:                      # noqa: BLE001
            dest.unlink(missing_ok=True)
            self.failed.emit(_friendly_error(e))
            return
        self.finished.emit(str(dest))

    def _verify(self, path: Path, got: int, sha: str):
        size = self.info.get("size") or 0
        if size and got != size:
            return "La descarga llegó incompleta. Inténtalo de nuevo."
        if self.info.get("sha256") and sha != self.info["sha256"]:
            return "El archivo descargado no coincide con el publicado (SHA-256). No se instala."
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return "Lo descargado no es un ejecutable de Windows. No se instala."
        return None


def swap_in(new_file: str) -> Path:
    """Pone el exe nuevo en el sitio del actual. Devuelve la ruta a lanzar. Lanza OSError si falla
    (y en ese caso deja todo como estaba)."""
    exe = exe_path()
    old = exe.with_name(exe.name + ".old")
    old.unlink(missing_ok=True)
    exe.rename(old)                         # un exe en marcha se puede renombrar, no sobrescribir
    try:
        Path(new_file).replace(exe)
    except OSError:
        old.rename(exe)                     # deshacer
        raise
    return exe


def launch(exe: Path):
    env = dict(os.environ)
    # PyInstaller (onefile): que el nuevo proceso no reutilice la carpeta temporal de este
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for k in ("_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
        env.pop(k, None)
    flags = 0x00000008 | 0x00000200         # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env, close_fds=True,
                     creationflags=flags if sys.platform == "win32" else 0)


def open_release_page(info: dict | None = None):
    QDesktopServices.openUrl(QUrl((info or {}).get("page") or RELEASES_PAGE))


# ---------------------------------------------------------------------------- diálogo

class UpdateDialog(QDialog):
    """Muestra la versión nueva y sus novedades. Resultado:
       accepted -> exe descargado en self.downloaded (listo para swap_in)
       rejected -> más tarde;  self.skipped = True -> omitir esta versión."""

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.info = info
        self.skipped = False
        self.downloaded: str | None = None
        self._dl: Downloader | None = None
        self.setWindowTitle("Actualización de WT Nav")
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        title = QLabel(f"<b>Hay una versión nueva: v{info['version']}</b> &nbsp; (tienes v{VERSION})")
        lay.addWidget(title)

        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setMarkdown(info["notes"] or "_Sin notas de la versión._")
        notes.setMinimumHeight(180)
        lay.addWidget(notes, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.hide()
        lay.addWidget(self.bar)

        btns = QHBoxLayout()
        self.b_skip = QPushButton("Omitir esta versión")
        self.b_skip.clicked.connect(self._skip)
        self.b_later = QPushButton("Más tarde")
        self.b_later.clicked.connect(self._later)
        can_install = is_frozen() and bool(info.get("url"))
        self.b_go = QPushButton("Actualizar ahora" if can_install else "Abrir página de descarga")
        self.b_go.setDefault(True)
        self.b_go.clicked.connect(self._start if can_install else self._open_page)
        btns.addWidget(self.b_skip)
        btns.addStretch(1)
        btns.addWidget(self.b_later)
        btns.addWidget(self.b_go)
        lay.addLayout(btns)

        if not is_frozen():
            self.status.setText("Estás ejecutando el código fuente: descarga la versión nueva a mano.")
        elif not info.get("url"):
            self.status.setText(f"Esta versión no incluye {EXE_ASSET}. Descárgala desde la página.")

    def _skip(self):
        self.skipped = True
        self.reject()

    def _later(self):
        if self._dl:
            self._dl.cancel()
        self.reject()

    def _open_page(self):
        open_release_page(self.info)
        self.reject()

    def _start(self):
        self.b_go.setEnabled(False)
        self.b_skip.setEnabled(False)
        self.b_later.setText("Cancelar")
        self.bar.show()
        self.bar.setRange(0, 0)
        self.status.setText("Descargando...")
        self._dl = Downloader(self.info, self)
        self._dl.progress.connect(self._on_progress, Qt.QueuedConnection)
        self._dl.finished.connect(self._on_done, Qt.QueuedConnection)
        self._dl.failed.connect(self._on_failed, Qt.QueuedConnection)
        self._dl.start()

    def _on_progress(self, got, total):
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(got)
            self.bar.setFormat(f"{got / 1e6:.1f} / {total / 1e6:.1f} MB")

    def _on_done(self, path):
        self.downloaded = path
        self.status.setText("Descargada. La app se reiniciará con la versión nueva.")
        self.accept()

    def _on_failed(self, msg):
        self._dl = None
        self.bar.hide()
        self.b_later.setText("Más tarde")
        self.b_skip.setEnabled(True)
        if not msg:                     # cancelada por el usuario
            self.reject()
            return
        self.status.setText(f"<span style='color:#ff8a7a'>{msg}</span>")
        self.b_go.setText("Reintentar")
        self.b_go.setEnabled(True)

    def closeEvent(self, e):
        if self._dl:
            self._dl.cancel()
        super().closeEvent(e)
