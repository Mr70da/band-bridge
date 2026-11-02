"""Small always-on-top desktop widget: steps ring, kcal, last night's sleep, band battery.

Polls the bridge's /fitness/summary every 60 s. `python widget.py --demo` uses fake data and no API.
"""
import json
import os
import sys
import threading
import tkinter as tk
import urllib.request
import webbrowser
from pathlib import Path

PORT = int(os.environ.get("BAND_BRIDGE_PORT", 8765))
POS_FILE = Path("data/widget.json")
W, H, KEY = 220, 150, "#010203"  # KEY is made transparent so the card can have rounded corners
BG, FG, MUT, TRACK, RING = "#171b22", "#e8ecf1", "#8a94a3", "#2b3340", "#3ddc97"
STAGE = {"deep": "#4a3fd0", "light": "#7aa2ff", "rem": "#c77dff", "awake": "#ff9f5a"}
DOT = {"ready": "#3ddc97", "connecting": "#ffb020", "backoff": "#ffb020"}


def ring_extent(steps, goal):
    """Arc sweep in degrees, clockwise; capped so a full ring still draws."""
    return min(max(steps, 0) / max(goal, 1), 1) * 359.9


def duration(minutes):
    return f"{minutes // 60}h {minutes % 60:02d}m"


def stage_widths(segments, width):
    """[(stage, pixel width)] for the thin sleep bar."""
    total = sum(s["end"] - s["start"] for s in segments)
    return [(s["stage"], (s["end"] - s["start"]) / total * width) for s in segments] if total else []


def load_pos():
    try:
        return json.loads(POS_FILE.read_text())
    except (OSError, ValueError):
        return None


class Widget:
    def __init__(self, demo=False):
        self.demo, self.data, self.error = demo, None, None
        self.root = r = tk.Tk()
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.configure(bg=KEY)
        r.attributes("-transparentcolor", KEY)
        pos = load_pos() or {"x": r.winfo_screenwidth() - W - 24, "y": r.winfo_screenheight() - H - 70}
        r.geometry(f"{W}x{H}+{pos['x']}+{pos['y']}")
        self.c = tk.Canvas(r, width=W, height=H, bg=KEY, highlightthickness=0)
        self.c.pack()
        self.menu = tk.Menu(r, tearoff=0)
        self.menu.add_command(label="Open dashboard", command=lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/dashboard"))
        self.menu.add_command(label="Refresh", command=self.refresh)
        self.menu.add_command(label="Close", command=r.destroy)
        self.c.bind("<ButtonPress-1>", lambda e: setattr(self, "grab", (e.x, e.y)))
        self.c.bind("<B1-Motion>", lambda e: r.geometry(f"+{r.winfo_x() + e.x - self.grab[0]}+{r.winfo_y() + e.y - self.grab[1]}"))
        self.c.bind("<ButtonRelease-1>", lambda e: self.save_pos())
        self.c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        self.draw()
        self.refresh()
        self.tick()

    def save_pos(self):
        try:
            POS_FILE.parent.mkdir(exist_ok=True)
            POS_FILE.write_text(json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}))
        except OSError:
            pass

    def refresh(self):
        def work():
            try:
                if self.demo:
                    from bridge import dashboard
                    self.data = dashboard.summary(dashboard.demo_store(), status=lambda: {"state": "ready", "battery": 72})
                else:
                    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/fitness/summary", timeout=3) as r:
                        self.data = json.load(r)
                self.error = None
            except Exception as e:
                self.error = e
        threading.Thread(target=work, daemon=True).start()

    def tick(self):  # redraw twice a second (cheap) so worker results show; refetch every 60 s
        self.draw()
        self.n = getattr(self, "n", 0) + 1
        if self.n % 120 == 0:
            self.refresh()
        self.root.after(500, self.tick)

    def draw(self):
        c, d = self.c, self.data
        c.delete("all")
        c.create_polygon(self._round(2, 2, W - 2, H - 2, 18), smooth=True, fill=BG, outline="#262c36")
        if not d:
            c.create_text(W / 2, H / 2, text="Bridge not running" if self.error else "Loading...", fill=MUT, font=("Segoe UI", 11))
            return
        steps, goal = d["today"]["steps"], d.get("goal") or 8000
        c.create_oval(14, 16, 84, 86, outline=TRACK, width=8)
        c.create_arc(14, 16, 84, 86, start=90, extent=-ring_extent(steps, goal), style="arc", outline=RING, width=8)
        c.create_text(49, 48, text=f"{steps:,}", fill=FG, font=("Segoe UI", 12, "bold"))
        c.create_text(49, 64, text=f"/ {goal:,}", fill=MUT, font=("Segoe UI", 7))
        c.create_text(98, 30, anchor="w", text=f"{d['today']['calories']:,} kcal", fill=FG, font=("Segoe UI", 12, "bold"))
        c.create_text(98, 52, anchor="w", text=f"{d['today']['distance'] / 1000:.1f} km", fill=MUT, font=("Segoe UI", 10))
        t = d["sleep_totals"]
        asleep = t["deep"] + t["light"] + t["rem"]
        c.create_text(14, 104, anchor="w", text=f"Sleep  {duration(asleep)}" if d["sleep"] else "No sleep recorded", fill=FG if d["sleep"] else MUT,
                      font=("Segoe UI", 9))
        x = 14
        for stage, w in stage_widths(d["sleep"], W - 28):
            c.create_rectangle(x, 114, x + w, 120, fill=STAGE[stage], width=0)
            x += w
        if not d["sleep"]:
            c.create_rectangle(14, 114, W - 14, 120, fill=TRACK, width=0)
        extra = [f"SpO2 {d['spo2_latest']}%"] if d.get("spo2_latest") is not None else []
        extra += [f"Stress {d['stress_latest']}"] if d.get("stress_latest") is not None else []
        c.create_text(W - 14, 135, anchor="e", font=("Segoe UI", 8), fill=MUT, text="   ".join(extra))
        online = d.get("state") in DOT
        c.create_oval(14, 131, 22, 139, fill=DOT.get(d.get("state"), "#ff5c5c"), width=0)
        c.create_text(28, 135, anchor="w", font=("Segoe UI", 8), fill=MUT,
                      text=(f"{d['battery']}%" if d.get("battery") is not None else "") + ("" if online else "  offline"))

    @staticmethod
    def _round(x1, y1, x2, y2, r):
        return [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
                x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent)  # data/ is relative; also makes `bridge` importable for --demo
    sys.path.insert(0, os.getcwd())
    Widget(demo="--demo" in sys.argv).root.mainloop()
