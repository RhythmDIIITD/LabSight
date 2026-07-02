"""
LabSight Pi Agent — runs on each Raspberry Pi locker unit.
Connects to the central laptop server via Socket.IO WebSocket.
All hardware logic (TFT, relays, RFID, camera) is unchanged from
WEBSITE_LAUNCHER.py — this file just wraps it in a client role.

Configuration (edit the three constants below):
    SERVER_URL  — laptop IP / hostname on your LAN
    LOCKER_ID   — unique name shown on the dashboard ("Locker 01", etc.)

Install extra dependency (Flask-SocketIO client):
    pip install "python-socketio[client]" eventlet

Run:
    python pi_agent.py
"""

import socketio as sio_client   # pip install "python-socketio[client]"
import threading, time, cv2, json, os, numpy as np
from datetime import datetime
from collections import deque
import onnxruntime as ort
from mfrc522 import MFRC522
from gpiozero import PWMOutputDevice
import RPi.GPIO as GPIO

# ─── TFT DISPLAY IMPORTS ─────────────────────────────────────────────────────
import board
import busio
import displayio
from fourwire import FourWire
import adafruit_ili9341
from adafruit_display_text import label
from adafruit_display_shapes.rect import Rect
from terminalio import FONT

# ══════════════════════════════════════════════════════════════════════════════
#  ▶ EDIT THESE THREE LINES
# ══════════════════════════════════════════════════════════════════════════════
SERVER_URL = "http://100.74.131.140:5000"   # laptop IP on your LAN
LOCKER_ID  = "Table 11"                   # unique name for this unit
# ══════════════════════════════════════════════════════════════════════════════

# ─── Socket.IO client ────────────────────────────────────────────────────────
sio = sio_client.Client(reconnection=True, reconnection_attempts=0,
                        reconnection_delay=2, reconnection_delay_max=10)

# ─── HARDWARE INIT ────────────────────────────────────────────────────────────
buzzer = PWMOutputDevice(12)

GPIO.setmode(GPIO.BCM)
GPIO.setup(5,  GPIO.OUT); GPIO.output(5,  GPIO.HIGH)
GPIO.setup(26, GPIO.OUT); GPIO.output(26, GPIO.HIGH)

displayio.release_displays()
_spi = busio.SPI(clock=board.SCK, MOSI=board.MOSI)
_display_bus = FourWire(_spi, command=board.D16, chip_select=board.D8, reset=board.D17)
tft_display = adafruit_ili9341.ILI9341(_display_bus, width=240, height=320, rotation=90)
tft_display.auto_refresh = False

W, H = 240, 320
TEAL  = 0x3FADA8
DARK  = 0x333333
WHITE = 0xFFFFFF
LGRAY = 0xE0E0E0
MGRAY = 0x808080
TABLE_NO = 11          # ← change per unit if needed

HEADER_H = 30
FOOTER_H = 30
SUB_H    = 20
BODY_TOP       = HEADER_H + SUB_H + 2
BODY_TOP_NOSUB = HEADER_H + 6

# ─── STUDENTS ─────────────────────────────────────────────────────────────────
STUDENTS = {
    737188003040: ("Rhythm Dahiya",  "2024457"),
    776188069219: ("Priyansh Malu",  "2024439"),
}

def get_display_string(card_id):
    entry = STUDENTS.get(card_id)
    if entry:
        return f"{entry[0]} ({entry[1]})"
    return f"Unknown ({card_id})"

# ─── RELAY ────────────────────────────────────────────────────────────────────
_relay5_off_time = 0.0

def _relay5_pulse():
    global _relay5_off_time
    GPIO.output(5, GPIO.LOW)
    _relay5_off_time = time.monotonic() + 3.0

def _relay5_off():
    global _relay5_off_time
    GPIO.output(5, GPIO.HIGH)
    _relay5_off_time = 0.0

def _relay26_on():  GPIO.output(26, GPIO.LOW)
def _relay26_off(): GPIO.output(26, GPIO.HIGH)

# ─── BUZZER ───────────────────────────────────────────────────────────────────
def buzzer_beep(count=2, freq=2500, on_ms=80, off_ms=50):
    def _beep():
        for _ in range(count):
            buzzer.frequency = freq; buzzer.value = 0.5
            time.sleep(on_ms / 1000); buzzer.off()
            time.sleep(off_ms / 1000)
    threading.Thread(target=_beep, daemon=True).start()

# ─── TFT HELPERS  (identical to original — not repeated for brevity) ──────────
# All build_screen_* functions, _tft_update, tft helpers are copy-pasted
# unchanged from WEBSITE_LAUNCHER.py.  They live below this comment block.
# ------ PASTE YOUR FULL TFT SECTION FROM WEBSITE_LAUNCHER.py HERE ------
# (build_screen_idle, build_screen_scan, build_screen_students,
#  build_screen_active, build_screen_returns, build_screen_all_returned,
#  build_screen_summary, _tft_update, _tft_show, _header, _footer, etc.)
# The section is identical — zero changes needed.

def _fmt_mins(total_mins):
    h, m = divmod(abs(int(total_mins)), 60)
    return "{:02d}:{:02d}".format(h, m)

def _fmt_time(t):
    h, m = t.tm_hour, t.tm_min
    ampm = " AM" if h < 12 else " PM"
    h = h % 12 or 12
    return "{:02d}:{:02d}{}".format(h, m, ampm)

def _wrap_text(text, max_chars):
    words = text.split()
    lines, current = [], ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current); current = word
    if current: lines.append(current)
    return "\n".join(lines)

def _header(group, clock_text=""):
    group.append(Rect(0, 0, W, HEADER_H + 5, fill=TEAL))
    group.append(label.Label(FONT, text="LabSight", color=WHITE, x=6, y=(HEADER_H // 2), scale=2))
    clk = label.Label(FONT, text=clock_text, color=WHITE, x=135, y=(HEADER_H // 2), scale=2)
    group.append(clk)
    return clk

def _footer(group, left_text, right_text=""):
    group.append(Rect(0, H - FOOTER_H + 10, (W // 3) - 18, FOOTER_H - 10, fill=DARK))
    group.append(label.Label(FONT, text=left_text, color=WHITE, x=6, y=H - (FOOTER_H // 3), scale=1))
    if right_text:
        rw = len(right_text) * 12
        group.append(label.Label(FONT, text=right_text, color=WHITE, x=W - rw - 6, y=H - (FOOTER_H // 2), scale=2))

def _white_bg(group): group.append(Rect(0, 0, W, H, fill=WHITE))

def _centred_label(group, text, y, color=TEAL, scale=2):
    lbl = label.Label(FONT, text=text, color=color, x=0, y=y, scale=scale)
    lbl.x = max(0, (W - lbl.bounding_box[2] * scale) // 2)
    group.append(lbl)
    return lbl

def _row_line(group, y): group.append(Rect(8, y, W - 16, 1, fill=LGRAY))

def _tft_show(group, clk=None):
    if clk: clk.text = _fmt_time(time.localtime())
    tft_display.root_group = group
    tft_display.refresh()

def build_screen_idle():
    g = displayio.Group(); _white_bg(g); clk = _header(g)
    mid = BODY_TOP + (H - BODY_TOP - FOOTER_H) // 2
    _centred_label(g, "No active", mid - 20, color=TEAL, scale=3)
    _centred_label(g, "session",   mid + 20, color=TEAL, scale=3)
    _footer(g, "Table {}".format(TABLE_NO))
    return g, clk

def build_screen_scan(lab_title=""):
    g = displayio.Group(); _white_bg(g); clk = _header(g)
    wrapped = _wrap_text(lab_title, 38)
    title_y = HEADER_H + 15
    g.append(label.Label(FONT, text=wrapped, color=DARK, x=6, y=title_y, scale=1, line_spacing=1.2))
    line_count = len(wrapped.split("\n"))
    title_bottom = title_y + line_count * 15
    g.append(Rect(0, title_bottom, W, 1, fill=DARK))
    remaining = H - title_bottom - FOOTER_H
    msg_y = title_bottom + (remaining // 2) - 10
    _centred_label(g, "Scan ID cards &",   msg_y,      color=TEAL, scale=2)
    _centred_label(g, "wait for briefing", msg_y + 22, color=TEAL, scale=2)
    _footer(g, "Table {}".format(TABLE_NO))
    return g, clk

def build_screen_students(lab_title="", students=None):
    students = students or []
    g = displayio.Group(); _white_bg(g); clk = _header(g)
    wrapped = _wrap_text(lab_title, 38)
    title_y = HEADER_H + 15
    g.append(label.Label(FONT, text=wrapped, color=DARK, x=6, y=title_y, scale=1, line_spacing=1.2))
    line_count = len(wrapped.split("\n"))
    title_bottom = title_y + line_count * 15
    g.append(Rect(0, title_bottom, W, 1, fill=DARK))
    y = title_bottom + 12
    g.append(Rect(0, y - 6, W, 18, fill=TEAL))
    g.append(label.Label(FONT, text="Student Name", color=WHITE, x=6,   y=y + 2, scale=1))
    g.append(label.Label(FONT, text="Roll No",      color=WHITE, x=182, y=y + 2, scale=1))
    y += 22
    prompt = "Scan ID cards and wait for briefing"
    wrapped_p = _wrap_text(prompt, 19)
    p_lines = len(wrapped_p.split("\n"))
    max_y = H - FOOTER_H - p_lines * 24 - 10
    for s in students:
        if y + 14 > max_y: break
        g.append(label.Label(FONT, text=s.get("name","")[:24], color=DARK, x=6,   y=y, scale=1))
        g.append(label.Label(FONT, text=s.get("roll","")[:8],  color=DARK, x=182, y=y, scale=1))
        y += 14; _row_line(g, y); y += 8
    bz_top = max_y + 5
    bz_h   = H - FOOTER_H - bz_top
    msg_y  = bz_top + (bz_h // 2) - ((p_lines - 1) * 12)
    for i, line in enumerate(wrapped_p.split("\n")):
        _centred_label(g, line, msg_y + i * 24, color=TEAL, scale=2)
    _footer(g, "Table {}".format(TABLE_NO))
    return g, clk

def build_screen_active(lab_title="", student_count=0, components=None, ends_in_mins=120):
    components = components or []
    g = displayio.Group(); _white_bg(g); clk = _header(g)
    wrapped = _wrap_text(lab_title, 38)
    title_y = HEADER_H + 15
    g.append(label.Label(FONT, text=wrapped, color=DARK, x=6, y=title_y, scale=1, line_spacing=1.2))
    line_count = len(wrapped.split("\n"))
    title_bottom = title_y + line_count * 15
    g.append(Rect(0, title_bottom, W, 1, fill=DARK))
    y = title_bottom + 12
    _centred_label(g, "Components Unlocked", y, color=TEAL, scale=2)
    y += 18
    g.append(Rect(0, y, W, 20, fill=DARK))
    g.append(label.Label(FONT, text="Component Name", color=WHITE, x=6,   y=y + 10, scale=1))
    g.append(label.Label(FONT, text="Count",          color=WHITE, x=198, y=y + 10, scale=1))
    y += 26
    max_y = H - FOOTER_H - 15
    for comp in components:
        if y + 14 > max_y: break
        name = comp.get("name","")[:24]
        count_val = str(comp.get("taken", comp.get("required", 0)))
        rw = len(count_val) * 6
        g.append(label.Label(FONT, text=name,      color=DARK, x=6,           y=y, scale=1))
        g.append(label.Label(FONT, text=count_val, color=DARK, x=W - rw - 12, y=y, scale=1))
        y += 14; _row_line(g, y); y += 8
    _footer(g, "Table {}".format(TABLE_NO), _fmt_mins(ends_in_mins))
    return g, clk

def build_screen_returns(lab_title="", components=None, ends_in_mins=120):
    components = components or []
    RED = 0xFF0000
    g = displayio.Group(); _white_bg(g); clk = _header(g)
    wrapped = _wrap_text(lab_title, 38)
    title_y = HEADER_H + 15
    g.append(label.Label(FONT, text=wrapped, color=DARK, x=6, y=title_y, scale=1, line_spacing=1.2))
    line_count = len(wrapped.split("\n"))
    title_bottom = title_y + line_count * 15
    g.append(Rect(0, title_bottom, W, 1, fill=DARK))
    y = title_bottom + 16
    sub_text = "Start Returning Components"
    wrapped_sub = _wrap_text(sub_text, 18)
    sub_lines = len(wrapped_sub.split("\n"))
    for i, line in enumerate(wrapped_sub.split("\n")):
        _centred_label(g, line, y + i * 24, color=TEAL, scale=2)
    y += sub_lines * 24
    g.append(Rect(0, y, W, 1, fill=LGRAY)); y += 12
    g.append(Rect(0, y - 6, W, 20, fill=DARK))
    g.append(label.Label(FONT, text="Component", color=WHITE, x=6,   y=y + 4, scale=1))
    g.append(label.Label(FONT, text="Status",    color=WHITE, x=198, y=y + 4, scale=1))
    y += 22
    max_y = H - FOOTER_H - 15
    for comp in components:
        if y + 14 > max_y: break
        name   = comp.get("name","")[:18]
        status = comp.get("status","pending")
        taken  = comp.get("taken", comp.get("required", 0))
        if status == "returned":
            s_text = "{}/{}".format(taken, taken); s_color = TEAL
        else:
            returned = comp.get("returned", 0)
            missing  = taken - returned
            s_text  = ("{}/{} - {} missing".format(returned, taken, missing) if missing > 0
                       else "{}/{}".format(returned, taken))
            s_color = RED if missing > 0 else TEAL
        rw = len(s_text) * 6
        g.append(label.Label(FONT, text=name,   color=DARK,    x=6,           y=y, scale=1))
        g.append(label.Label(FONT, text=s_text, color=s_color, x=W - rw - 12, y=y, scale=1))
        y += 14; _row_line(g, y); y += 8
    _footer(g, "Table {}".format(TABLE_NO), _fmt_mins(ends_in_mins))
    return g, clk

def build_screen_all_returned():
    g = displayio.Group(); _white_bg(g); clk = _header(g)
    mid = BODY_TOP_NOSUB + (H - BODY_TOP_NOSUB - FOOTER_H) // 2
    _centred_label(g, "All components", mid - 90, color=TEAL, scale=2)
    _centred_label(g, "returned",       mid - 65, color=TEAL, scale=2)
    g.append(Rect(16, mid - 30, W - 32, 1, fill=DARK))
    _centred_label(g, "Close lid and press", mid,      color=DARK, scale=2)
    _centred_label(g, "LOCK button",         mid + 30, color=TEAL, scale=2)
    _centred_label(g, "to end session",      mid + 60, color=DARK, scale=2)
    _footer(g, "Table {}".format(TABLE_NO))
    return g, clk

def build_screen_summary(lab_title="", students=None, components=None, elapsed_mins=0, roll_frame=0):
    students   = students or []
    components = components or []
    RED = 0xFF0000
    g = displayio.Group(); _white_bg(g); clk = _header(g)
    title_y = HEADER_H + 15
    _centred_label(g, "Session Ended", title_y, color=DARK, scale=2)
    y = title_y + 18
    wrapped = _wrap_text(lab_title, 38)
    g.append(label.Label(FONT, text=wrapped, color=TEAL, x=6, y=y, scale=1, line_spacing=1.2))
    y += len(wrapped.split("\n")) * 15
    g.append(label.Label(FONT, text="Duration: {}".format(_fmt_mins(elapsed_mins)), color=MGRAY, x=6, y=y, scale=1))
    y += 14
    g.append(Rect(0, y, W, 1, fill=DARK)); y += 12
    max_y = H - FOOTER_H - 15
    if y + 35 <= max_y:
        g.append(label.Label(FONT, text="Attended Students", color=DARK, x=6, y=y, scale=1)); y += 14
        g.append(Rect(0, y, W, 1, fill=LGRAY)); y += 10
        for s in students[:3]:
            if y + 14 > max_y - 45: break
            g.append(label.Label(FONT, text=s.get("name","")[:24], color=DARK, x=6,   y=y, scale=1))
            g.append(label.Label(FONT, text=s.get("roll","")[:8],  color=DARK, x=182, y=y, scale=1))
            y += 14; _row_line(g, y); y += 8
    y += 4
    if y + 30 <= max_y:
        g.append(Rect(0, y, W, 20, fill=DARK))
        g.append(label.Label(FONT, text="Component",    color=WHITE, x=6,   y=y + 10, scale=1))
        g.append(label.Label(FONT, text="Final Status", color=WHITE, x=168, y=y + 10, scale=1))
        y += 26
        row_h = 22
        max_visible = (max_y - y) // row_h
        total = len(components)
        if total <= max_visible:
            visible = components
        else:
            start = roll_frame % total
            visible = (components[start:] + components[:start])[:max_visible]
        for comp in visible:
            if y + 14 > max_y: break
            name   = comp.get("name","")[:18]
            status = comp.get("status","pending")
            taken  = comp.get("taken", comp.get("required", 0))
            if status == "returned":
                s_text = "{}/{}".format(taken, taken); s_color = TEAL
            else:
                returned = comp.get("returned", 0)
                missing  = taken - returned
                s_text  = ("{}/{} - {} lost".format(returned, taken, missing) if missing > 0
                           else "{}/{}".format(returned, taken))
                s_color = RED if missing > 0 else TEAL
            rw = len(s_text) * 6
            g.append(label.Label(FONT, text=name,   color=DARK,    x=6,           y=y, scale=1))
            g.append(label.Label(FONT, text=s_text, color=s_color, x=W - rw - 12, y=y, scale=1))
            y += 14; _row_line(g, y); y += 8
    _footer(g, "Table {}".format(TABLE_NO))
    return g, clk

tft_state = {"screen": 0, "roll_frame": 0, "last_roll": time.monotonic()}
ROLL_INTERVAL = 1.8

def _tft_update(screen_num, lab_title="", students=None, components=None,
                elapsed_mins=0, ends_in_mins=120):
    g, clk = None, None
    if screen_num == 1:   g, clk = build_screen_idle()
    elif screen_num == 2: g, clk = build_screen_scan(lab_title)
    elif screen_num == 3: g, clk = build_screen_students(lab_title, students)
    elif screen_num == 4: g, clk = build_screen_active(lab_title, components=components, ends_in_mins=ends_in_mins)
    elif screen_num == 5: g, clk = build_screen_returns(lab_title, components, ends_in_mins)
    elif screen_num == 6: g, clk = build_screen_all_returned()
    elif screen_num == 7: g, clk = build_screen_summary(lab_title, students, components, elapsed_mins, tft_state["roll_frame"])
    if g: _tft_show(g, clk)

# ─── ONNX + Camera ────────────────────────────────────────────────────────────
LABEL_MAP      = {0: "Capacitor", 1: "IC", 2: "Resistor", 3: "Transistor"}
CONF_THRESHOLD = 0.60
AVERAGE_WINDOW = 8
AUTO_ALLOW_SECONDS = 300

ort_session = ort.InferenceSession("best.onnx", providers=["CPUExecutionProvider"])
INPUT_NAME  = ort_session.get_inputs()[0].name

cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 640)

detection_history = deque(maxlen=AVERAGE_WINDOW)

def _preprocess(frame):
    img = cv2.resize(frame, (640, 640))
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    return np.transpose(img, (2, 0, 1))[np.newaxis, :]

def _postprocess(output):
    counts = {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0}
    preds = output[0]
    if preds.ndim == 3: preds = preds[0]
    if preds.shape[0] < preds.shape[1]: preds = preds.T
    boxes = preds[:, :4]
    class_scores = preds[:, 4:]
    confs = class_scores.max(axis=1)
    classes = class_scores.argmax(axis=1)
    for conf, cls in zip(confs, classes):
        if conf >= CONF_THRESHOLD:
            label_name = LABEL_MAP.get(int(cls))
            if label_name: counts[label_name] += 1
    return counts

def get_averaged_counts():
    if not detection_history:
        return {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0}
    sums = {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0}
    for fc in detection_history:
        for lbl in sums: sums[lbl] += fc[lbl]
    n = len(detection_history)
    return {lbl: round(sums[lbl] / n) for lbl in sums}

# ─── Shared experiment state ──────────────────────────────────────────────────
reader = None

experiment = {
    "phase":             "idle",
    "title":             "",
    "started_at":        "",
    "max_students":      2,
    "components":        {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0},
    "scanned_ids":       [],
    "scanned_names":     [],
    "session_active":    False,
    "experiment_active": False,
    "locking_allowed":   False,
    "locked":            False,
    "elapsed_seconds":   0,
    "final_time":        0,
    "final_detected":    {},
    "detected":          {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0},
    "all_found_at":      None,
    "detection_frozen":  False,
    "frozen_detected":   {},
}

# ─── State snapshot (Pi → Server) ────────────────────────────────────────────
def _build_state_snapshot():
    det  = experiment["detected"]
    req  = experiment["components"]
    comp_status = {}
    for comp in ["Capacitor", "IC", "Resistor", "Transistor"]:
        h, n = det[comp], req[comp]
        if not experiment["experiment_active"] and not experiment["locked"]:
            status = "idle"
        elif h > n:   status = "extra"
        elif n == 0:  status = "unused"
        elif h >= n:  status = "ok"
        else:         status = "missing"
        comp_status[comp] = {"detected": h, "required": n, "status": status}

    m, s = divmod(experiment["elapsed_seconds"], 60)

    final_stats = None
    if experiment["locked"]:
        req2 = experiment["components"]
        det2 = experiment["final_detected"]
        ft   = experiment["final_time"]
        rows = []
        for comp in ["Capacitor", "IC", "Resistor", "Transistor"]:
            n, h = req2[comp], det2.get(comp, 0)
            if h > n:   rows.append({"comp": comp, "label": f"{h}/{n}", "note": f"+{h-n} extra",   "state": "extra"})
            elif n == 0: continue
            elif h < n: rows.append({"comp": comp, "label": f"{h}/{n}", "note": f"missing {n-h}", "state": "missing"})
            else:       rows.append({"comp": comp, "label": f"{h}/{n} ✓", "note": "", "state": "ok"})
        final_stats = {
            "title":    experiment["title"] or "Untitled Session",
            "duration": f"{ft//60:02d}:{ft%60:02d}",
            "students": ", ".join(experiment["scanned_names"]) or "None",
            "rows":     rows,
            "frozen":   experiment["detection_frozen"],
        }

    return {
        "locker_id":         LOCKER_ID,
        "phase":             experiment["phase"],
        "title":             experiment["title"],
        "locking_allowed":   experiment["locking_allowed"],
        "timer":             f"{m:02d}:{s:02d}",
        "scanned_names":     experiment["scanned_names"],
        "max_students":      experiment["max_students"],
        "comp_status":       comp_status,
        "final_stats":       final_stats,
        "detection_frozen":  experiment["detection_frozen"],
    }

# ─── Command handler (Server → Pi) ───────────────────────────────────────────
@sio.on("command")
def on_command(data):
    """Receive commands from the laptop dashboard."""
    global reader
    cmd = data.get("cmd")

    if cmd == "new_session":
        experiment.update({
            "phase": "idle", "title": "", "started_at": "", "max_students": 2,
            "components": {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0},
            "scanned_ids": [], "scanned_names": [], "session_active": False,
            "experiment_active": False, "locking_allowed": False, "locked": False,
            "elapsed_seconds": 0, "final_time": 0, "final_detected": {},
            "detected": {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0},
            "all_found_at": None, "detection_frozen": False, "frozen_detected": {},
        })
        detection_history.clear()
        _relay5_off(); _relay26_off()

    elif cmd == "start_session":
        experiment["title"]          = data.get("title", "").strip()
        experiment["started_at"]     = datetime.now().strftime("%d %b %Y, %H:%M")
        experiment["components"]     = data.get("components", experiment["components"])
        experiment["max_students"]   = int(data.get("max_students", 2))
        experiment["session_active"] = True
        experiment["phase"]          = "session"
        if not reader:
            try:
                reader = MFRC522(bus=1, device=0, spd=1000000, pin_rst=25)
                print("[RFID] Initialised on SPI1")
            except Exception as e:
                print(f"[RFID] Init failed: {e}")
        buzzer_beep(count=2, on_ms=100, off_ms=60)

    elif cmd == "start_experiment":
        experiment["experiment_active"] = True
        experiment["phase"]             = "experiment"
        experiment["all_found_at"]      = None
        experiment["detection_frozen"]  = False
        buzzer_beep(count=2, on_ms=100, off_ms=60)
        _relay5_pulse()

    elif cmd == "allow_lock":
        experiment["locking_allowed"] = True
        _relay26_on()

    elif cmd == "lock":
        _do_lock()

    elif cmd == "clear_session":
        # Show summary/locked screen briefly, then reset to idle after 10 s
        threading.Thread(target=_delayed_reset, daemon=True).start()

# ─── Delayed reset to idle (called after clear_session) ──────────────────────
def _delayed_reset():
    """Wait 10 seconds (so the summary screen is visible), then reset to idle."""
    time.sleep(10)
    experiment.update({
        "phase": "idle", "title": "", "started_at": "", "max_students": 2,
        "components": {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0},
        "scanned_ids": [], "scanned_names": [], "session_active": False,
        "experiment_active": False, "locking_allowed": False, "locked": False,
        "elapsed_seconds": 0, "final_time": 0, "final_detected": {},
        "detected": {"Capacitor": 0, "IC": 0, "Resistor": 0, "Transistor": 0},
        "all_found_at": None, "detection_frozen": False, "frozen_detected": {},
    })
    detection_history.clear()
    _relay5_off()
    _relay26_off()
    # tft_worker will pick up phase == "idle" and show screen 1 on its next tick

# ─── Lock + report ───────────────────────────────────────────────────────────
def _build_report():
    req = experiment["components"]
    det = experiment["final_detected"]
    rows = []
    for comp in ["Capacitor", "IC", "Resistor", "Transistor"]:
        n, h = req[comp], det.get(comp, 0)
        if n == 0: continue
        if h > n:   rows.append({"comp": comp, "detected": h, "required": n, "state": "extra",   "note": f"+{h-n} extra"})
        elif h < n: rows.append({"comp": comp, "detected": h, "required": n, "state": "missing", "note": f"missing {n-h}"})
        else:       rows.append({"comp": comp, "detected": h, "required": n, "state": "ok",      "note": ""})
    ft = experiment["final_time"]
    return {
        "id":         datetime.now().strftime("%Y%m%d_%H%M%S"),
        "locker_id":  LOCKER_ID,
        "title":      experiment["title"] or "Untitled Session",
        "date":       datetime.now().strftime("%d %b %Y, %H:%M"),
        "started_at": experiment["started_at"],
        "duration":   f"{ft//60:02d}:{ft%60:02d}",
        "students":   list(experiment["scanned_names"]),
        "required":   dict(req),
        "detected":   dict(det),
        "rows":       rows,
    }

def _do_lock():
    if not experiment["experiment_active"]: return
    experiment["experiment_active"] = False
    experiment["session_active"]    = False
    experiment["locked"]            = True
    experiment["locking_allowed"]   = True
    experiment["final_time"]        = experiment["elapsed_seconds"]
    experiment["final_detected"]    = dict(experiment["frozen_detected"])
    experiment["phase"]             = "locked"
    buzzer_beep(count=3, on_ms=150, off_ms=80)
    _relay26_off()
    # Send completed report to server
    report = _build_report()
    sio.emit("report_ready", {"report": report})

# ─── Background workers (unchanged logic) ─────────────────────────────────────

def camera_worker():
    while True:
        if experiment["experiment_active"]:
            ret, frame = cap.read()
            if ret:
                inp = _preprocess(frame)
                out = ort_session.run(None, {INPUT_NAME: inp})
                counts = _postprocess(out)
                detection_history.append(counts)
                if not experiment["detection_frozen"]:
                    experiment["detected"] = get_averaged_counts()
                    if experiment["locking_allowed"]:
                        det = experiment["detected"]
                        req = experiment["components"]
                        all_found = all(
                            req[c] > 0 and det[c] >= req[c]
                            for c in ["Capacitor", "IC", "Resistor", "Transistor"]
                            if req[c] > 0
                        ) and any(req[c] > 0 for c in req)
                        if all_found:
                            if experiment["all_found_at"] is None:
                                experiment["all_found_at"] = time.monotonic()
                            elif time.monotonic() - experiment["all_found_at"] >= 5.0:
                                experiment["detection_frozen"] = True
                                experiment["frozen_detected"]  = dict(experiment["detected"])
                        else:
                            experiment["all_found_at"] = None
                    else:
                        experiment["all_found_at"] = None
            time.sleep(0.3)
        else:
            time.sleep(0.3)

def rfid_worker():
    last_id = None; last_rendered_n = -1; loop_count = 0
    while True:
        active = experiment["session_active"] and not experiment["experiment_active"]
        if not active:
            last_id = None; last_rendered_n = -1; loop_count = 0
            time.sleep(0.3); continue
        current_n  = len(experiment["scanned_names"])
        screen_num = 3 if current_n > 0 else 2
        if current_n != last_rendered_n:
            comp_list, students_list, lab_title, elapsed_mins, ends_in_mins = _build_tft_args()
            _tft_update(screen_num, lab_title, students_list, comp_list, elapsed_mins, ends_in_mins)
            last_rendered_n = current_n
        if reader is None: time.sleep(0.5); continue
        card_id = None
        try:
            (status, _) = reader.MFRC522_Request(reader.PICC_REQIDL)
            if status == reader.MI_OK:
                (status, uid) = reader.MFRC522_Anticoll()
                if status == reader.MI_OK:
                    n = 0
                    for i in range(5): n = n * 256 + uid[i]
                    card_id = n
        except Exception as e:
            card_id = None
        loop_count += 1
        if card_id and card_id != last_id:
            last_id = card_id
            if card_id not in experiment["scanned_ids"] and \
               len(experiment["scanned_ids"]) < experiment["max_students"]:
                display_name = get_display_string(card_id)
                experiment["scanned_ids"].append(card_id)
                experiment["scanned_names"].append(display_name)
                buzzer_beep(count=1, on_ms=120, off_ms=0)
        elif not card_id:
            last_id = None
        time.sleep(0.3)

def timer_worker():
    while True:
        if experiment["experiment_active"]:
            time.sleep(1)
            experiment["elapsed_seconds"] += 1
            if experiment["elapsed_seconds"] >= AUTO_ALLOW_SECONDS and \
               not experiment["locking_allowed"]:
                experiment["locking_allowed"] = True
        else:
            time.sleep(0.2)

def _build_tft_args():
    phase = experiment["phase"]
    comp_list = [
        {"name": c, "required": experiment["components"][c],
         "taken": experiment["components"][c], "status": "pending"}
        for c in ["Capacitor", "IC", "Resistor", "Transistor"]
        if experiment["components"][c] > 0
    ]
    if phase == "experiment" and experiment["locking_allowed"]:
        det = experiment["detected"]
        for c in comp_list:
            h = det.get(c["name"], 0); n = c["required"]
            c["taken"] = n; c["returned"] = h
            c["status"] = "returned" if h >= n else "pending"
    elif phase == "locked":
        det = experiment.get("final_detected", {})
        for c in comp_list:
            h = det.get(c["name"], 0); n = c["required"]
            c["taken"] = n; c["returned"] = h
            c["status"] = "returned" if h >= n else "missing"
    students_list = [
        {"name": s.split(" (")[0], "roll": s.split("(")[-1].rstrip(")")}
        for s in experiment["scanned_names"]
    ]
    lab_title    = experiment["title"] or "Lab Session"
    elapsed_secs = experiment["final_time"] if phase == "locked" else experiment["elapsed_seconds"]
    elapsed_mins = elapsed_secs / 60
    ends_in_mins = max(0, (AUTO_ALLOW_SECONDS - experiment["elapsed_seconds"]) / 60)
    return comp_list, students_list, lab_title, elapsed_mins, ends_in_mins

def tft_worker():
    last_screen = -1
    while True:
        phase = experiment["phase"]
        now   = time.monotonic()
        if _relay5_off_time > 0.0 and now >= _relay5_off_time:
            _relay5_off()
        screen_num = 1
        if   phase == "idle":       screen_num = 1
        elif phase == "session":    screen_num = 3 if experiment["scanned_names"] else 2
        elif phase == "experiment":
            if experiment["detection_frozen"]:   screen_num = 6
            elif experiment["locking_allowed"]:  screen_num = 5
            else:                                screen_num = 4
        elif phase == "locked":     screen_num = 7

        if screen_num != last_screen:
            if screen_num not in (2, 3): buzzer_beep(count=1, on_ms=80)
            last_screen = screen_num
            tft_state["roll_frame"] = 0; tft_state["last_roll"] = now
            if screen_num not in (2, 3):
                comp_list, students_list, lab_title, elapsed_mins, ends_in_mins = _build_tft_args()
                _tft_update(screen_num, lab_title, students_list, comp_list, elapsed_mins, ends_in_mins)
        elif screen_num == 7 and (now - tft_state["last_roll"]) >= ROLL_INTERVAL:
            tft_state["roll_frame"] += 1; tft_state["last_roll"] = now
            comp_list, students_list, lab_title, elapsed_mins, ends_in_mins = _build_tft_args()
            _tft_update(7, lab_title, students_list, comp_list, elapsed_mins, ends_in_mins)
        elif screen_num == 5 and (now - tft_state["last_roll"]) >= 1.0:
            tft_state["last_roll"] = now
            comp_list, students_list, lab_title, elapsed_mins, ends_in_mins = _build_tft_args()
            _tft_update(5, lab_title, students_list, comp_list, elapsed_mins, ends_in_mins)
        time.sleep(0.5)

# ─── State pusher (Pi → Server every 1 s) ────────────────────────────────────
def state_push_worker():
    while True:
        try:
            if sio.connected:
                sio.emit("state_update", {
                    "locker_id": LOCKER_ID,
                    "state":     _build_state_snapshot(),
                })
        except Exception as e:
            print(f"[State push] {e}")
        time.sleep(1)

# ─── Heartbeat worker ─────────────────────────────────────────────────────────
def heartbeat_worker():
    while True:
        try:
            if sio.connected:
                sio.emit("heartbeat", {"locker_id": LOCKER_ID})
        except Exception as e:
            print(f"[Heartbeat] {e}")
        time.sleep(5)

# ─── Socket.IO connection events ─────────────────────────────────────────────
@sio.event
def connect():
    print(f"[Agent] Connected to server. Registering as '{LOCKER_ID}'")
    sio.emit("register", {"locker_id": LOCKER_ID})

@sio.event
def disconnect():
    print("[Agent] Disconnected from server. Will auto-reconnect.")

@sio.on("registered")
def on_registered(data):
    print(f"[Agent] Server confirmed registration: {data}")

# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _prime_g, _prime_clk = build_screen_idle()
    _tft_show(_prime_g, _prime_clk)
    try:
        threading.Thread(target=camera_worker,     daemon=True).start()
        threading.Thread(target=rfid_worker,       daemon=True).start()
        threading.Thread(target=timer_worker,      daemon=True).start()
        threading.Thread(target=tft_worker,        daemon=True).start()
        threading.Thread(target=heartbeat_worker,  daemon=True).start()
        threading.Thread(target=state_push_worker, daemon=True).start()

        print(f"[Agent] Connecting to {SERVER_URL} ...")
        sio.connect(SERVER_URL, wait_timeout=10)
        sio.wait()   # blocks forever; workers run in background threads
    finally:
        buzzer.off()
        GPIO.cleanup()