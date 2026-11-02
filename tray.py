"""Band Bridge with a system-tray icon: the HTTP API plus a dot that shows the band's state.

Run with pythonw.exe (no console): logs go to logs/bridge-YYYYmmdd.log. Do not run it together with
`python -m bridge.api`: both want port 8765 and the single BLE connection.
"""
import json
import logging
import os
import re
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

PORT = int(os.environ.get("BAND_BRIDGE_PORT", 8765))
GREEN, AMBER, RED = (46, 160, 67), (230, 150, 0), (210, 40, 40)
COLORS = {"ready": GREEN, "connecting": AMBER, "backoff": AMBER}  # anything else (needs_pairing, stopped, error): red

log = logging.getLogger("tray")


def color_for(state):
    return COLORS.get(state, RED)


def tooltip(state, battery=None):
    return f"Band Bridge: {state}" + (f" (battery {battery}%)" if battery is not None else "")


def dot(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((6, 6, 58, 58), fill=color)
    return img


class Tray:
    def __init__(self):
        import pystray
        self.state, self.battery, self.paired_warned, self.quitting = "starting", None, False, False
        self.icon = pystray.Icon("band-bridge", dot(RED), tooltip(self.state), pystray.Menu(
            pystray.MenuItem("Status", self.show_status),
            pystray.MenuItem("Open log folder", lambda: os.startfile(Path("logs").resolve())),
            pystray.MenuItem("Quit", self.quit)))
        self.server = None

    def on_state(self, state, detail=""):  # called from the server thread
        self.state = state
        if m := re.search(r"battery (\d+)%", detail):
            self.battery = int(m[1])
        self.icon.icon, self.icon.title = dot(color_for(state)), tooltip(state, self.battery)
        if state == "needs_pairing" and not self.paired_warned:
            self.paired_warned = True
            self.icon.notify("Band needs pairing: run connect.py")

    def show_status(self):
        try:  # asks the running API, so connects/disconnects come from the Supervisor
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=3) as r:
                s = json.load(r)
            text = (f"{s['state']}, battery {s['battery']}%, "
                    f"{s['stats']['connects']} connects, {s['stats']['disconnects']} disconnects")
        except Exception as e:
            text = f"{tooltip(self.state, self.battery)} (API not answering: {e!r})"
        self.icon.notify(text, "Band Bridge")

    def serve(self):
        import uvicorn
        from bridge.api import create_app
        try:
            self.server = uvicorn.Server(uvicorn.Config(
                create_app(on_state=self.on_state), host="127.0.0.1", port=PORT, log_config=None))
            self.server.run()  # sets up its own event loop in this thread
        except BaseException:  # a busy port makes uvicorn call sys.exit
            log.exception("bridge server failed")
        if not self.quitting:
            self.on_state("error")
            self.icon.notify(f"Bridge stopped: is port {PORT} busy? See the log.")

    def quit(self):
        self.quitting = True

        def stop():
            if self.server:
                self.server.should_exit = True  # lifespan shutdown makes the Supervisor close the BLE session
            self.thread.join(30)
            self.icon.stop()
        threading.Thread(target=stop).start()

    def run(self):
        def setup(icon):
            icon.visible = True
            self.thread = threading.Thread(target=self.serve, name="bridge")
            self.thread.start()
        self.icon.run(setup)


def main():
    os.chdir(Path(__file__).resolve().parent)  # config/ and logs/ are relative; Task Scheduler starts elsewhere
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[
        logging.FileHandler(datetime.now().strftime("logs/bridge-%Y%m%d.log"), encoding="utf-8")])
    Tray().run()


if __name__ == "__main__":
    main()
