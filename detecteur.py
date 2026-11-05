#!/usr/bin/env python3
"""
Détecteur de mouvement : surveille une zone de l'écran et entoure en rouge
ce qui bouge dedans. Fait pour KDE Plasma 6 sous Wayland, en consommant
le moins possible sur batterie.

  python detecteur.py          lance tout
  python detecteur.py --zone   redéfinir la zone (depuis un autre terminal ou un raccourci)

- raccourci global proposé au premier lancement : Meta+Alt+Z
- icône dans la zone de notification : clic = nouvelle zone, clic droit = pause / quitter
- au premier lancement KDE demande quel écran partager, le choix est retenu ensuite
"""
import json
import os
import secrets
import signal
import socket
import sys
import threading
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "xcb"  # XWayland : sinon impossible de placer l'overlay au-dessus de tout
os.environ["QT_NO_GLIB"] = "1"         # la boucle GLib (D-Bus, GStreamer) tourne dans son propre thread

import numpy as np  # noqa: E402
import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gio, GLib, Gst, GstVideo  # noqa: E402

from PyQt6.QtCore import QObject, QRectF, QSocketNotifier, Qt, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget  # noqa: E402

# --- réglages ---
FPS_MAX = 8                # images par seconde max demandées à KWin
LARGEUR_ANALYSE = 160      # la zone est réduite à ~160 px de large avant l'analyse
SEUIL_PIXEL = 25           # écart de luminosité (0-255) pour qu'un pixel compte comme "bougé"
CASE = 8                   # taille d'une case de la grille (en px réduits)
SEUIL_CASE = 0.08          # part de pixels bougés pour qu'une case s'allume
DUREE_AFFICHAGE_MS = 700   # le rectangle reste affiché ce temps après le dernier mouvement
RACCOURCI = "LOGO+ALT+z"   # Meta+Alt+Z
VIOLET = QColor(97, 0, 137)
ROUGE = QColor(255, 50, 50)

APP_ID = "io.github.glime32.DetecteurMouvement"
CONF_DIR = Path.home() / ".config" / "detecteur-mouvement"
TOKEN_FILE = CONF_DIR / "restore_token"
ZONE_FILE = CONF_DIR / "zone.json"
PID_FILE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "detecteur-mouvement.pid"
DESKTOP_FILE = Path.home() / ".local" / "share" / "applications" / f"{APP_ID}.desktop"

PORTAIL = "org.freedesktop.portal.Desktop"
PORTAIL_CHEMIN = "/org/freedesktop/portal/desktop"


def v(type_, valeur):
    return GLib.Variant(type_, valeur)


# --- portails XDG (capture d'écran + raccourci global) ---
class Portail:
    """Sous Wayland, la seule façon propre de capturer l'écran c'est le portail."""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.emetteur = self.bus.get_unique_name()[1:].replace(".", "_")
        self.sessions = []  # il faut garder les sessions en vie

    def _requete(self, interface, methode, construire):
        """Appelle une méthode du portail et attend le signal Response
        (le temps que l'utilisateur réponde au dialogue KDE)."""
        jeton = "dm" + secrets.token_hex(6)
        chemin = f"{PORTAIL_CHEMIN}/request/{self.emetteur}/{jeton}"
        boucle, resultat = GLib.MainLoop(), {}

        def reponse(_c, _e, _p, _i, _s, params):
            resultat["code"], resultat["res"] = params.unpack()
            boucle.quit()

        sid = self.bus.signal_subscribe(None, "org.freedesktop.portal.Request", "Response",
                                        chemin, None, Gio.DBusSignalFlags.NONE, reponse)
        self.bus.call_sync(PORTAIL, PORTAIL_CHEMIN, interface, methode, construire(jeton),
                           None, Gio.DBusCallFlags.NONE, -1, None)
        boucle.run()
        self.bus.signal_unsubscribe(sid)
        if resultat["code"] != 0:
            raise RuntimeError(f"{methode} refusé ou annulé (code {resultat['code']})")
        return resultat["res"]

    def _session(self, interface):
        res = self._requete(interface, "CreateSession", lambda j: v("(a{sv})", ({
            "handle_token": v("s", j),
            "session_handle_token": v("s", "dm" + secrets.token_hex(6)),
        },)))
        self.sessions.append(res["session_handle"])
        return res["session_handle"]

    def enregistrer_app(self):
        # hors Flatpak, le raccourci global a besoin d'un app id enregistré
        try:
            self.bus.call_sync(PORTAIL, PORTAIL_CHEMIN, "org.freedesktop.host.portal.Registry", "Register",
                               v("(sa{sv})", (APP_ID, {})), None, Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as e:
            # pas bloquant, le raccourci marche souvent quand même
            print("Info : enregistrement auprès du portail impossible :", e.message)

    def demarrer_capture(self):
        itf = "org.freedesktop.portal.ScreenCast"
        session = self._session(itf)
        options = {
            "types": v("u", 1),          # un écran entier
            "multiple": v("b", False),
            "cursor_mode": v("u", 1),    # sans le curseur, sinon il déclenche tout seul
            "persist_mode": v("u", 2),   # retenir l'écran choisi
        }
        if TOKEN_FILE.exists():
            options["restore_token"] = v("s", TOKEN_FILE.read_text().strip())
        self._requete(itf, "SelectSources",
                      lambda j: v("(oa{sv})", (session, {**options, "handle_token": v("s", j)})))
        res = self._requete(itf, "Start",
                            lambda j: v("(osa{sv})", (session, "", {"handle_token": v("s", j)})))
        if "restore_token" in res:
            TOKEN_FILE.write_text(res["restore_token"])
        noeud = res["streams"][0][0]
        retour, fds = self.bus.call_with_unix_fd_list_sync(
            PORTAIL, PORTAIL_CHEMIN, itf, "OpenPipeWireRemote", v("(oa{sv})", (session, {})),
            GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, -1, None, None)
        return fds.get(retour.unpack()[0]), noeud

    def lier_raccourci(self, rappel):
        itf = "org.freedesktop.portal.GlobalShortcuts"
        session = self._session(itf)
        raccourcis = [("zone", {
            "description": v("s", "Détecteur de mouvement : définir la zone"),
            "preferred_trigger": v("s", RACCOURCI),
        })]
        self._requete(itf, "BindShortcuts", lambda j: v("(oa(sa{sv})sa{sv})",
                                                        (session, raccourcis, "", {"handle_token": v("s", j)})))

        def active(_c, _e, _p, _i, _s, params):
            if params.unpack()[1] == "zone":
                rappel()

        self.bus.signal_subscribe(None, itf, "Activated", PORTAIL_CHEMIN, None,
                                  Gio.DBusSignalFlags.NONE, active)


# --- capture et détection ---
def regrouper(grille):
    """Fusionne les cases allumées voisines en rectangles (x0, y0, x1, y1), en cases."""
    gh, gw = grille.shape
    vu = np.zeros_like(grille)
    boites = []
    for y, x in np.argwhere(grille):
        if vu[y, x]:
            continue
        pile, vu[y, x] = [(y, x)], True
        x0 = x1 = x
        y0 = y1 = y
        while pile:
            cy, cx = pile.pop()
            x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
            # on tolère un trou d'une case entre deux morceaux
            for ny in range(max(cy - 2, 0), min(cy + 3, gh)):
                for nx in range(max(cx - 2, 0), min(cx + 3, gw)):
                    if grille[ny, nx] and not vu[ny, nx]:
                        vu[ny, nx] = True
                        pile.append((ny, nx))
        boites.append((x0, y0, x1 + 1, y1 + 1))
    return boites


class Capteur:
    """Reçoit les images de KWin par PipeWire.

    KWin n'envoie une image que quand l'écran change (et pas plus de
    FPS_MAX par seconde), donc écran figé = script qui dort."""

    def __init__(self, fd, noeud):
        self.quand_mouvement = lambda boites: None
        self.quand_erreur = lambda msg: None
        self.verrou = threading.Lock()
        self.zone = None          # (x, y, l, h) entre 0 et 1
        self.prec = None
        self.reinit = True
        self.dernier = 0.0
        self.traits = []          # nos propres rectangles récents, à ignorer
        self.etat = None
        self.pipeline = Gst.parse_launch(
            f"pipewiresrc fd={fd} path={noeud} do-timestamp=true ! "
            f"video/x-raw,format={{BGRx,BGRA,RGBx,RGBA}},max-framerate={FPS_MAX}/1 ! "
            "appsink name=sortie emit-signals=true max-buffers=1 drop=true sync=false")
        self.pipeline.get_by_name("sortie").connect("new-sample", self._image)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", lambda _b, m: self.quand_erreur(m.parse_error()[0].message))

    def definir_zone(self, zone):
        with self.verrou:
            self.zone, self.reinit = zone, True

    def noter_traits(self, boites):
        t = time.monotonic()
        with self.verrou:
            self.traits.extend((b, t) for b in boites)

    def actif(self, oui):
        # en pause, KWin arrête carrément d'envoyer des images
        etat = Gst.State.PLAYING if oui else Gst.State.PAUSED
        if etat != self.etat:
            self.etat = etat
            with self.verrou:
                self.reinit = True
            self.pipeline.set_state(etat)

    def arreter(self):
        self.pipeline.set_state(Gst.State.NULL)

    def _image(self, sortie):
        echantillon = sortie.emit("pull-sample")
        maintenant = time.monotonic()
        with self.verrou:
            zone = self.zone
            if self.reinit:
                self.prec, self.reinit = None, False
        if zone is None or maintenant - self.dernier < 1 / FPS_MAX:
            return Gst.FlowReturn.OK
        self.dernier = maintenant

        buf = echantillon.get_buffer()
        info = GstVideo.VideoInfo.new_from_caps(echantillon.get_caps())
        L, H = info.width, info.height
        try:
            meta = GstVideo.buffer_get_video_meta(buf)
            pas_ligne, decalage = (meta.stride[0], meta.offset[0]) if meta else (info.stride[0], 0)
        except Exception:
            pas_ligne, decalage = buf.get_size() // H, 0

        x0, y0 = int(zone[0] * L), int(zone[1] * H)
        x1, y1 = int((zone[0] + zone[2]) * L), int((zone[1] + zone[3]) * H)
        pas = max(1, (x1 - x0) // LARGEUR_ANALYSE)

        carte = buf.map(Gst.MapFlags.READ)
        ok, carte = carte if isinstance(carte, tuple) else (bool(carte), carte)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            brut = np.frombuffer(carte.data, np.uint8, count=pas_ligne * H, offset=decalage)
            # on ne garde que le vert (octet 1 en BGRx comme en RGBx) : ça suffit
            # comme luminosité et ça fait 4 fois moins de données
            gris = brut.reshape(H, pas_ligne)[y0:y1:pas, x0 * 4 + 1:x1 * 4:pas * 4].astype(np.int16)
        finally:
            buf.unmap(carte)

        boites = self._detecter(gris, maintenant)
        if boites:
            self.quand_mouvement(boites)
        return Gst.FlowReturn.OK

    def _detecter(self, gris, maintenant):
        prec, self.prec = self.prec, gris
        if prec is None or prec.shape != gris.shape:
            return []
        bouge = np.abs(gris - prec) > SEUIL_PIXEL
        h, w = bouge.shape

        # sans ça l'overlay se détecte lui-même et ça tourne en boucle
        with self.verrou:
            self.traits = [(b, t) for b, t in self.traits if maintenant - t < 0.6]
            traits = [b for b, _ in self.traits]
        e = 2
        for bx, by, bw, bh in traits:
            xa, ya = int(bx * w), int(by * h)
            xb, yb = int((bx + bw) * w), int((by + bh) * h)
            bouge[max(ya - e, 0):ya + e + 1, max(xa - e, 0):xb + e + 1] = False
            bouge[max(yb - e, 0):yb + e + 1, max(xa - e, 0):xb + e + 1] = False
            bouge[max(ya - e, 0):yb + e + 1, max(xa - e, 0):xa + e + 1] = False
            bouge[max(ya - e, 0):yb + e + 1, max(xb - e, 0):xb + e + 1] = False

        gh, gw = h // CASE, w // CASE
        if gh == 0 or gw == 0:
            return []
        cases = bouge[:gh * CASE, :gw * CASE].reshape(gh, CASE, gw, CASE).mean(axis=(1, 3)) > SEUIL_CASE
        if not cases.any():
            return []
        return [(a * CASE / w, b * CASE / h, (c - a) * CASE / w, (d - b) * CASE / h)
                for a, b, c, d in regrouper(cases)]


# --- fenêtres ---
class Overlay(QWidget):
    """Fenêtre plein écran transparente, toujours au-dessus, que les clics traversent."""

    def __init__(self, geometrie):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.X11BypassWindowManagerHint
                            | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setGeometry(geometrie)
        self.zone = None
        self.boites = []
        self.pause = False

    def paintEvent(self, _):
        if not self.zone:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        L, H = self.width(), self.height()
        zx, zy, zl, zh = self.zone[0] * L, self.zone[1] * H, self.zone[2] * L, self.zone[3] * H
        stylo = QPen(VIOLET, 2)
        if self.pause:
            stylo.setStyle(Qt.PenStyle.DashLine)
        p.setPen(stylo)
        p.setBrush(Qt.BrushStyle.NoBrush)
        # le cadre est dessiné juste à l'extérieur de la zone pour ne pas être analysé
        p.drawRoundedRect(QRectF(zx - 4, zy - 4, zl + 8, zh + 8), 6, 6)
        p.setPen(QPen(ROUGE, 2))
        for bx, by, bl, bh in self.boites:
            p.drawRoundedRect(QRectF(zx + bx * zl, zy + by * zh, bl * zl, bh * zh), 4, 4)
        p.end()


class Selection(QWidget):
    """Écran assombri sur lequel on trace la zone. Échap pour annuler."""
    terminee = pyqtSignal(object)  # zone entre 0 et 1, ou None

    def __init__(self, geometrie):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(geometrie)
        self.debut = self.fin = None
        self.fait = False

    def showEvent(self, e):
        self.activateWindow()
        self.setFocus()
        super().showEvent(e)

    def _finir(self, zone):
        if not self.fait:
            self.fait = True
            self.close()
            self.terminee.emit(zone)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key.Key_Escape:
            self._finir(None)

    def mousePressEvent(self, e):
        self.debut = self.fin = e.position()
        self.update()

    def mouseMoveEvent(self, e):
        if self.debut is not None:
            self.fin = e.position()
            self.update()

    def mouseReleaseEvent(self, e):
        if self.debut is None:
            return
        r = QRectF(self.debut, e.position()).normalized()
        if r.width() < 10 or r.height() < 10:  # simple clic = annuler
            self._finir(None)
            return
        L, H = self.width(), self.height()
        self._finir((r.x() / L, r.y() / H, r.width() / L, r.height() / H))

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))
        if self.debut is not None:
            r = QRectF(self.debut, self.fin).normalized()
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(r, Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(VIOLET, 2))
            p.drawRect(r)
        p.setPen(Qt.GlobalColor.white)
        p.drawText(self.rect().adjusted(0, 40, 0, 0),
                   Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                   "Trace la zone à surveiller  —  Échap pour annuler")
        p.end()


# --- application ---
class Appli(QObject):
    # les signaux Qt servent à repasser dans le thread de l'interface
    # depuis les threads GStreamer / D-Bus
    mouvement = pyqtSignal(object)
    zone_demandee = pyqtSignal()
    erreur = pyqtSignal(str)

    def __init__(self, app, capteur):
        super().__init__()
        self.app, self.capteur = app, capteur
        self.geometrie = app.primaryScreen().geometry()
        self.pause = False
        self.zone = None
        self.selection = None

        self.overlay = Overlay(self.geometrie)
        self.overlay.show()

        # un seul tir après le dernier mouvement, pas de timer qui tourne en boucle
        self.minuteur = QTimer(self)
        self.minuteur.setSingleShot(True)
        self.minuteur.setInterval(DUREE_AFFICHAGE_MS)
        self.minuteur.timeout.connect(self.effacer)

        self.mouvement.connect(self.sur_mouvement)
        self.zone_demandee.connect(self.choisir_zone)
        self.erreur.connect(self.sur_erreur)
        capteur.quand_mouvement = self.mouvement.emit
        capteur.quand_erreur = self.erreur.emit

        self._icone()
        try:
            zone = tuple(json.loads(ZONE_FILE.read_text()))
        except (OSError, ValueError):
            zone = None
        if zone:
            self.appliquer_zone(zone)
        else:
            QTimer.singleShot(300, self.choisir_zone)

    def _icone(self):
        self.tray = QSystemTrayIcon(QIcon.fromTheme("crosshairs", QIcon.fromTheme("edit-select")), self)
        self.menu = QMenu()
        self.menu.addAction("Définir la zone", self.choisir_zone)
        self.action_pause = self.menu.addAction("Pause", self.basculer_pause)
        self.menu.addAction("Quitter", self.app.quit)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("Détecteur de mouvement")
        self.tray.activated.connect(
            lambda r: self.choisir_zone() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _maj_capture(self):
        self.capteur.actif(self.zone is not None and not self.pause and self.selection is None)

    def appliquer_zone(self, zone):
        self.zone = zone
        self.overlay.zone = zone
        self.capteur.definir_zone(zone)
        ZONE_FILE.write_text(json.dumps(zone))
        self._maj_capture()
        self.overlay.update()

    def choisir_zone(self):
        if self.selection is not None:
            return
        self.effacer()
        self.selection = Selection(self.geometrie)
        self.selection.terminee.connect(self._fin_selection)
        self._maj_capture()  # pas de capture pendant qu'on trace
        self.selection.showFullScreen()

    def _fin_selection(self, zone):
        self.selection.deleteLater()
        self.selection = None
        if zone:
            self.appliquer_zone(zone)
        else:
            self._maj_capture()

    def basculer_pause(self):
        self.pause = not self.pause
        self.action_pause.setText("Reprendre" if self.pause else "Pause")
        self.overlay.pause = self.pause
        self.effacer()
        self._maj_capture()
        self.overlay.update()

    def sur_mouvement(self, boites):
        if self.pause or self.selection is not None:
            return
        self.capteur.noter_traits(self.overlay.boites + boites)
        self.overlay.boites = boites
        self.overlay.update()
        self.minuteur.start()

    def effacer(self):
        if self.overlay.boites:
            self.capteur.noter_traits(self.overlay.boites)
            self.overlay.boites = []
            self.overlay.update()

    def sur_erreur(self, message):
        print("Capture interrompue :", message)
        self.app.quit()


def installer_fichier_desktop():
    if not DESKTOP_FILE.exists():
        DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP_FILE.write_text(
            "[Desktop Entry]\nType=Application\nName=Détecteur de mouvement\n"
            f"Exec=python {Path(__file__).resolve()}\nIcon=crosshairs\nNoDisplay=true\n")


def main():
    if "--zone" in sys.argv:
        try:
            os.kill(int(PID_FILE.read_text()), signal.SIGUSR1)
        except (OSError, ValueError):
            sys.exit("Le détecteur ne tourne pas.")
        return

    Gst.init(None)
    if Gst.ElementFactory.find("pipewiresrc") is None:
        sys.exit("Plugin GStreamer PipeWire manquant : sudo pacman -S gst-plugin-pipewire")

    CONF_DIR.mkdir(parents=True, exist_ok=True)
    installer_fichier_desktop()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setDesktopFileName(APP_ID)

    portail = Portail()
    portail.enregistrer_app()
    try:
        fd, noeud = portail.demarrer_capture()
    except Exception as e:
        sys.exit(f"Capture d'écran impossible : {e}")

    capteur = Capteur(fd, noeud)
    appli = Appli(app, capteur)

    try:
        portail.lier_raccourci(appli.zone_demandee.emit)
        print("Raccourci global actif (Meta+Alt+Z par défaut).")
    except Exception as e:
        print(f"Raccourci global indisponible ({e}).\n"
              "Utilise l'icône de notification, ou lie `python detecteur.py --zone` à un raccourci KDE.")

    threading.Thread(target=GLib.MainLoop().run, daemon=True).start()

    # SIGUSR1 = nouvelle zone, Ctrl+C / SIGTERM = quitter.
    # set_wakeup_fd évite d'avoir un timer Qt juste pour laisser Python voir les signaux.
    lecture, ecriture = socket.socketpair()
    lecture.setblocking(False)
    ecriture.setblocking(False)
    signal.set_wakeup_fd(ecriture.fileno())
    for s in (signal.SIGUSR1, signal.SIGINT, signal.SIGTERM):
        signal.signal(s, lambda *_: None)

    def sur_signal():
        for n in lecture.recv(64):
            if n == signal.SIGUSR1:
                appli.choisir_zone()
            elif n in (signal.SIGINT, signal.SIGTERM):
                app.quit()

    notif = QSocketNotifier(lecture.fileno(), QSocketNotifier.Type.Read)
    notif.activated.connect(sur_signal)

    PID_FILE.write_text(str(os.getpid()))
    try:
        code = app.exec()
    finally:
        capteur.arreter()
        PID_FILE.unlink(missing_ok=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
