"""
race_logger.py - records racing-game telemetry to CSV for the RaceIQ app.

Supported games (Windows PC):
    * Assetto Corsa              (reads the game's shared memory)
    * Assetto Corsa Competizione (reads the game's shared memory)
    * F1 25                      (listens for the game's UDP telemetry)

The script works out which game is running, then after every COMPLETED lap
it saves that lap to a CSV in the format RaceIQ reads: one row per ~1 m
"bin" around the lap (position, speed, pedals, steering, gear, rpm, ...).

Usage:
    python race_logger.py                 # auto-detect the game and record
    python race_logger.py --check         # show live readings for 20 s
    python race_logger.py --game f1       # force a game: ac | acc | f1 | auto
    python race_logger.py --out D:\\laps   # choose the output folder

Stop recording with Ctrl+C. Only completed laps are saved.
Needs: numpy (already in the RaceIQ requirements). See README.txt.
"""

import argparse
import collections
import csv
import ctypes
import mmap
import os
import re
import socket
import struct
import subprocess
import sys
import time
from datetime import datetime

import numpy as np


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

POLL_SECONDS = 0.01        # shared-memory polling interval (~100 times a second)
MIN_LAP_COVERAGE = 0.95    # a lap must span this much of the track to be saved
MAX_GAP_M = 25.0           # bins with no real sample within this distance are invalid
MIN_SAMPLES = 100          # ignore "laps" with fewer samples than this
F1_DEFAULT_PORT = 20777
F1_LAP_DEBOUNCE_S = 10.0   # ignore a second "new lap" signal within this many seconds

AC_PROCESSES = {"acs.exe", "acs_x86.exe"}
ACC_PROCESSES = {"ac2-win64-shipping.exe"}

AC_STATUS_LIVE = 2         # shared-memory status value while you are driving

# lapFlag bits written to the CSV (RaceIQ ignores flagged laps for best/consistency)
FLAG_INVALID = 1           # the game marked the lap invalid (track limits etc.)
FLAG_PIT = 2               # the lap involved the pit lane (in-lap / out-lap)


# One reading from a game. `sample` is a tuple in the S_* order defined below.
Frame = collections.namedtuple("Frame", "live lap sample car track flag restart")


# ---------------------------------------------------------------------------
# CSV format (matches the columns the RaceIQ app already reads)
# ---------------------------------------------------------------------------

FIELDS = [
    "carId", "trackId", "trackLength", "lapIndex", "lapNum", "lapFlag",
    "binIndex", "validBin", "lap_time",
    "world_position_X", "world_position_Y", "world_position_Z",
    "velocity_X", "velocity_Y", "velocity_Z", "speed",
    "gforce_X", "gforce_Y", "gforce_Z",
    "race_position", "throttle", "brake", "steering", "gear", "rpm",
    "game",
]

# Order of the values stored for each raw sample while a lap is being driven.
# Gear is stored already normalised: 1 = first gear, 0 = neutral, -1 = reverse.
S_POS, S_TIME, S_X, S_Y, S_Z, S_VX, S_VY, S_VZ, S_SPEED, S_GX, S_GY, S_GZ, \
    S_RACEPOS, S_GAS, S_BRAKE, S_STEER, S_GEAR, S_RPM = range(18)

CONTINUOUS = [S_TIME, S_X, S_Y, S_Z, S_VX, S_VY, S_VZ, S_SPEED,
              S_GX, S_GY, S_GZ, S_GAS, S_BRAKE, S_STEER, S_RPM]


# ---------------------------------------------------------------------------
# Lap processing: raw samples -> one row per ~1 m bin (shared by all games)
# ---------------------------------------------------------------------------

def process_lap(samples, lap_num, car, track, game, flag):
    """Turn the raw samples of one lap into evenly spaced rows.

    Returns (rows, info). rows is None if the lap isn't usable; info then
    holds a short reason.
    """
    if len(samples) < MIN_SAMPLES:
        return None, {"reason": f"only {len(samples)} samples"}

    a = np.array(samples, dtype=float)

    # Samples taken just before the start line carry a position near 1.0 while
    # the rest of the lap starts near 0.0. Split at those wraps and keep the
    # segment that covers the most track.
    pos_raw = a[:, S_POS]
    cuts = np.where(np.diff(pos_raw) < -0.5)[0] + 1
    segments = np.split(np.arange(len(pos_raw)), cuts)
    seg = max(segments, key=lambda s: pos_raw[s].max() - pos_raw[s].min())
    a = a[seg]

    # Position must only ever increase; drop repeats.
    pos = np.maximum.accumulate(a[:, S_POS])
    _, keep = np.unique(pos, return_index=True)
    a = a[keep]
    pos = pos[keep]

    coverage = float(pos[-1] - pos[0])
    if coverage < MIN_LAP_COVERAGE or len(pos) < MIN_SAMPLES:
        return None, {"reason": f"covered only {coverage * 100:.0f}% of the track"}

    # Track length from the distance actually driven, scaled up to a full lap
    xyz = a[:, [S_X, S_Y, S_Z]]
    path = float(np.sum(np.linalg.norm(np.diff(xyz, axis=0), axis=1)))
    length = path / coverage
    n_bins = int(round(length))
    if n_bins < 100:
        return None, {"reason": f"track length looks wrong ({length:.0f} m)"}

    # One bin roughly every metre, placed at the centre of each bin
    grid = (np.arange(n_bins) + 0.5) / n_bins
    grid_c = np.clip(grid, pos[0], pos[-1])

    cols = {}
    for c in CONTINUOUS:
        cols[c] = np.interp(grid_c, pos, a[:, c])

    # Gear and race position are whole numbers: take the nearest real sample
    right = np.clip(np.searchsorted(pos, grid_c), 1, len(pos) - 1)
    left = right - 1
    nearest = np.where(np.abs(pos[left] - grid_c) <= np.abs(pos[right] - grid_c), left, right)
    gear = a[nearest, S_GEAR]
    race_pos = a[nearest, S_RACEPOS]

    # A bin is only valid if real samples sit close on both sides of it
    gap_m = (pos[right] - pos[left]) * length
    inside = (grid >= pos[0]) & (grid <= pos[-1])
    valid = inside & (gap_m <= MAX_GAP_M)

    rows = []
    for i in range(n_bins):
        rows.append([
            car,
            track,
            round(length, 1),
            lap_num,
            lap_num,
            int(flag),
            i,
            int(valid[i]),
            round(cols[S_TIME][i], 3),
            round(cols[S_X][i], 3), round(cols[S_Y][i], 3), round(cols[S_Z][i], 3),
            round(cols[S_VX][i], 4), round(cols[S_VY][i], 4), round(cols[S_VZ][i], 4),
            round(cols[S_SPEED][i], 2),
            round(cols[S_GX][i], 4), round(cols[S_GY][i], 4), round(cols[S_GZ][i], 4),
            int(race_pos[i]),
            round(float(np.clip(cols[S_GAS][i], 0, 1)), 3),
            round(float(np.clip(cols[S_BRAKE][i], 0, 1)), 3),
            round(cols[S_STEER][i], 4),
            int(gear[i]),
            int(round(cols[S_RPM][i])),
            game,
        ])

    info = {
        "lap_time": float(a[-1, S_TIME]),
        "length": length,
        "bins": n_bins,
        "valid_bins": int(valid.sum()),
        "samples": len(a),
    }
    return rows, info


def fmt_time(seconds):
    m = int(seconds // 60)
    return f"{m}:{seconds - 60 * m:06.3f}"


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9_-]+", "_", str(text)).strip("_") or "unknown"


class CsvSession:
    """Creates the CSV on the first saved lap, then appends each lap to it."""

    def __init__(self, out_dir, game, car, track):
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{safe_name(game)}_{safe_name(track)}_{safe_name(car)}_{stamp}.csv"
        self.path = os.path.join(out_dir, name)
        self.wrote_header = False

    def write(self, rows):
        with open(self.path, "a", newline="") as f:
            w = csv.writer(f)
            if not self.wrote_header:
                w.writerow(FIELDS)
                self.wrote_header = True
            w.writerows(rows)


# ---------------------------------------------------------------------------
# Assetto Corsa / Assetto Corsa Competizione: shared memory
# Only the leading fields we need are declared (the real structures are
# longer, which is fine because we only read the start of each page).
# AC and ACC use the same page names but different graphics layouts.
# ---------------------------------------------------------------------------

class ACPhysics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("packetId", ctypes.c_int),
        ("gas", ctypes.c_float),
        ("brake", ctypes.c_float),
        ("fuel", ctypes.c_float),
        ("gear", ctypes.c_int),          # 0 = reverse, 1 = neutral, 2 = first ...
        ("rpms", ctypes.c_int),
        ("steerAngle", ctypes.c_float),
        ("speedKmh", ctypes.c_float),
        ("velocity", ctypes.c_float * 3),
        ("accG", ctypes.c_float * 3),
    ]


_GRAPHIC_PREFIX = [
    ("packetId", ctypes.c_int),
    ("status", ctypes.c_int),            # 0 off, 1 replay, 2 live, 3 pause
    ("session", ctypes.c_int),
    ("currentTime", ctypes.c_wchar * 15),
    ("lastTime", ctypes.c_wchar * 15),
    ("bestTime", ctypes.c_wchar * 15),
    ("split", ctypes.c_wchar * 15),
    ("completedLaps", ctypes.c_int),
    ("position", ctypes.c_int),
    ("iCurrentTime", ctypes.c_int),      # milliseconds into the current lap
    ("iLastTime", ctypes.c_int),
    ("iBestTime", ctypes.c_int),
    ("sessionTimeLeft", ctypes.c_float),
    ("distanceTraveled", ctypes.c_float),
    ("isInPit", ctypes.c_int),
    ("currentSectorIndex", ctypes.c_int),
    ("lastSectorTime", ctypes.c_int),
    ("numberOfLaps", ctypes.c_int),
    ("tyreCompound", ctypes.c_wchar * 33),
    ("replayTimeMultiplier", ctypes.c_float),
    ("normalizedCarPosition", ctypes.c_float),   # 0..1 around the lap
]


class ACGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = _GRAPHIC_PREFIX + [
        ("carCoordinates", ctypes.c_float * 3),
    ]


class ACCGraphic(ctypes.Structure):
    _pack_ = 4
    _fields_ = _GRAPHIC_PREFIX + [
        ("activeCars", ctypes.c_int),
        ("carCoordinates", (ctypes.c_float * 3) * 60),   # every car on track
        ("carID", ctypes.c_int * 60),
        ("playerCarID", ctypes.c_int),
    ]


class ACStatic(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("smVersion", ctypes.c_wchar * 15),
        ("acVersion", ctypes.c_wchar * 15),
        ("numberOfSessions", ctypes.c_int),
        ("numCars", ctypes.c_int),
        ("carModel", ctypes.c_wchar * 33),
        ("track", ctypes.c_wchar * 33),
    ]


def mapping_exists(tag):
    """True if the game has already created this shared-memory page.

    We check first because opening a mapping that doesn't exist would make
    Python create an empty one, which could interfere with the game.
    """
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    k32.OpenFileMappingW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.OpenFileMappingW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.OpenFileMappingW(0x0004, False, tag)   # FILE_MAP_READ
    if handle:
        k32.CloseHandle(handle)
        return True
    return False


class SharedMemorySource:
    """Reader for Assetto Corsa (game='ac') or ACC (game='acc')."""

    PAGES = {
        "physics": "acpmf_physics",
        "graphics": "acpmf_graphics",
        "static": "acpmf_static",
    }

    def __init__(self, game):
        self.game = game
        self.label = "Assetto Corsa" if game == "ac" else "Assetto Corsa Competizione"
        self.structs = {
            "physics": ACPhysics,
            "graphics": ACGraphic if game == "ac" else ACCGraphic,
            "static": ACStatic,
        }
        self.maps = {}
        self.last_packet = -1
        self.polls = 0
        self.car = "unknown"
        self.track = "unknown"

    def connect(self):
        """Try to attach to all three pages. Returns True when connected."""
        if self.maps:
            return True
        found = {}
        for key, name in self.PAGES.items():
            tag = next((t for t in (name, "Local\\" + name) if mapping_exists(t)), None)
            if tag is None:
                return False
            found[key] = mmap.mmap(0, ctypes.sizeof(self.structs[key]), tag,
                                   access=mmap.ACCESS_READ)
        self.maps = found
        self._refresh_static()
        return True

    def _read(self, key):
        struct_type = self.structs[key]
        mm = self.maps[key]
        mm.seek(0)
        return struct_type.from_buffer_copy(mm.read(ctypes.sizeof(struct_type)))

    def _refresh_static(self):
        s = self._read("static")
        self.car = s.carModel or "unknown"
        self.track = s.track or "unknown"

    def info(self):
        s = self._read("static")
        return [
            f"Game: {self.label}",
            f"Car: {s.carModel}   Track: {s.track}",
            f"Game version: {s.acVersion}   Shared-memory version: {s.smVersion}",
        ]

    def summary(self):
        return []

    def _player_coordinates(self, g):
        if self.game == "ac":
            return g.carCoordinates
        # ACC lists every car; find the player's entry
        n = max(0, min(int(g.activeCars), 60))
        for i in range(n):
            if g.carID[i] == g.playerCarID:
                return g.carCoordinates[i]
        return g.carCoordinates[0]

    def poll(self):
        g = self._read("graphics")
        if g.packetId == self.last_packet:
            time.sleep(POLL_SECONDS)
            return None
        self.last_packet = g.packetId

        p = self._read("physics")
        self.polls += 1
        if self.polls % 300 == 1:
            self._refresh_static()

        c = self._player_coordinates(g)
        sample = (
            float(g.normalizedCarPosition),
            g.iCurrentTime / 1000.0,
            float(c[0]), float(c[1]), float(c[2]),
            float(p.velocity[0]), float(p.velocity[1]), float(p.velocity[2]),
            float(p.speedKmh),
            float(p.accG[0]), float(p.accG[1]), float(p.accG[2]),
            int(g.position),
            float(p.gas), float(p.brake), float(p.steerAngle),
            int(p.gear) - 1,                 # 1 = first gear, 0 = neutral, -1 = reverse
            int(p.rpms),
        )
        return Frame(
            live=(g.status == AC_STATUS_LIVE),
            lap=int(g.completedLaps),
            sample=sample,
            car=self.car,
            track=self.track,
            flag=FLAG_PIT if g.isInPit else 0,
            restart=False,
        )

    def close(self):
        for mm in self.maps.values():
            mm.close()
        self.maps = {}


# ---------------------------------------------------------------------------
# F1 25: UDP telemetry
# Layouts follow EA's official spec. Two layouts are supported:
#   "22" = formats 2024 / 2025:  22 cars, float g-forces
#   "24" = format 2026 (F1 25 2026 Season Pack): 24 cars, quantised g-forces
# Every packet's size is checked against the spec so a mismatch is reported
# instead of silently producing wrong numbers.
# ---------------------------------------------------------------------------

HEADER = struct.Struct("<HBBBBBQfIIBB")
assert HEADER.size == 29

F1_LAYOUT = {2024: "22", 2025: "22", 2026: "24"}
F1_CARS = {"22": 22, "24": 24}
F1_PACKET_SIZES = {          # packet id -> total datagram size
    "22": {0: 1349, 2: 1285, 6: 1352},
    "24": {0: 1325, 2: 1399, 6: 1448},
}
F1_MOTION_BYTES = {"22": 60, "24": 54}
F1_LAP_BYTES = 57
F1_TELEMETRY_BYTES = {"22": 60, "24": 59}

F1_MOTION = {"22": struct.Struct("<6f6h3f3f"), "24": struct.Struct("<6f6h3h3f")}
F1_LAP = struct.Struct("<II12xfff14B")
F1_TELEMETRY = struct.Struct("<HfffBbH")

F1_TRACKS = {
    0: "melbourne", 2: "shanghai", 3: "sakhir", 4: "catalunya", 5: "monaco",
    6: "montreal", 7: "silverstone", 9: "hungaroring", 10: "spa", 11: "monza",
    12: "singapore", 13: "suzuka", 14: "abu_dhabi", 15: "texas", 16: "brazil",
    17: "austria", 19: "mexico", 20: "baku", 26: "zandvoort", 27: "imola",
    29: "jeddah", 30: "miami", 31: "las_vegas", 32: "losail",
    39: "silverstone_reverse", 40: "austria_reverse", 41: "zandvoort_reverse",
    42: "madrid",
}


class F1Source:
    game = "f1"
    label = "F1 25"

    def __init__(self, port=F1_DEFAULT_PORT):
        self.port = port
        self.sock = None
        self.counts = collections.Counter()
        self.warned = set()
        self.fmt = None
        self.version = None
        self.track_length = None
        self.track_id = None
        self.motion = None        # (frame id, parsed values)
        self.lapdata = None
        self.telemetry = None
        # lap tracking
        self.lap_id = 0
        self.last_lapnum = None
        self.prev_dist = None
        self.last_trigger_time = -1e9
        self.session_time = 0.0

    # --- network -----------------------------------------------------------

    def open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        sock.bind(("0.0.0.0", self.port))
        sock.settimeout(0.05)
        self.sock = sock

    def _recv(self):
        try:
            data, _ = self.sock.recvfrom(4096)
            return data
        except (socket.timeout, OSError):
            return None

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    # --- detection -----------------------------------------------------------

    @staticmethod
    def _valid_header(data):
        if len(data) < HEADER.size:
            return False
        fmt, year, _, _, _, pid = HEADER.unpack_from(data)[:6]
        return 2020 <= fmt <= 2030 and pid <= 20

    def probe(self, seconds=1.0):
        """True if F1 telemetry packets arrive within `seconds`."""
        end = time.time() + seconds
        while time.time() < end:
            data = self._recv()
            if data and self._valid_header(data):
                return True
        return False

    # --- reporting -----------------------------------------------------------

    def info(self):
        return [
            f"Game: F1 25 (listening for UDP telemetry on port {self.port})",
            "Waiting for packets... (UDP telemetry must be ON in the game's settings)",
        ]

    def summary(self):
        lines = []
        if self.fmt:
            year, major, minor = self.version
            lines.append(f"UDP format {self.fmt}, game version {major}.{minor:02d}")
        if self.counts:
            names = {0: "motion", 1: "session", 2: "lap data", 6: "car telemetry"}
            parts = [f"{names.get(k, 'packet ' + str(k))}: {v}" for k, v in sorted(self.counts.items())]
            lines.append("Packets received: " + ", ".join(parts))
        return lines

    # --- packet handling -------------------------------------------------------

    def _warn(self, key, text):
        if key not in self.warned:
            self.warned.add(key)
            print("  WARNING: " + text)

    def handle(self, data):
        """Process one datagram. Returns a Frame when a full sample is ready."""
        if not self._valid_header(data):
            return None
        h = HEADER.unpack_from(data)
        fmt, year, major, minor, _, pid, _, session_time, frame, _, player, _ = h
        self.counts[pid] += 1
        self.fmt = fmt
        self.version = (year, major, minor)
        self.session_time = session_time

        layout = F1_LAYOUT.get(fmt)
        if layout is None:
            self._warn(("fmt", fmt),
                       f"UDP format {fmt} is not supported. In F1 25 set UDP Format to 2026 "
                       "(or 2025) in the telemetry settings.")
            return None

        if pid == 1:    # session: track length and track id
            if len(data) >= 37:
                self.track_length = struct.unpack_from("<H", data, 33)[0]
                self.track_id = struct.unpack_from("<b", data, 36)[0]
            return None

        if pid not in (0, 2, 6):
            return None

        expected = F1_PACKET_SIZES[layout][pid]
        if len(data) != expected:
            self._warn(("size", fmt, pid, len(data)),
                       f"packet type {pid} has size {len(data)}, expected {expected} for format {fmt}. "
                       "Skipping these packets. Please report this message.")
            return None
        if player >= F1_CARS[layout]:
            return None

        if pid == 0:
            off = HEADER.size + player * F1_MOTION_BYTES[layout]
            self.motion = (frame, F1_MOTION[layout].unpack_from(data, off), layout)
        elif pid == 2:
            off = HEADER.size + player * F1_LAP_BYTES
            self.lapdata = (frame, F1_LAP.unpack_from(data, off))
        else:
            off = HEADER.size + player * F1_TELEMETRY_BYTES[layout]
            self.telemetry = (frame, F1_TELEMETRY.unpack_from(data, off))
            return self._build_frame(frame, year)
        return None

    def _build_frame(self, frame, year):
        if not (self.motion and self.lapdata and self.telemetry and self.track_length):
            return None
        if abs(self.motion[0] - frame) > 1 or abs(self.lapdata[0] - frame) > 1:
            return None

        m, layout = self.motion[1], self.motion[2]
        (last_ms, cur_ms, dist, _total, _sc, carpos, lapnum, pit, _npit, _sector, invalid,
         _pen, _warn, _cc, _dt, _sg, _grid, driver_status, _result) = self.lapdata[1]
        speed, throttle, steer, brake, _clutch, gear, rpm = self.telemetry[1]

        length = float(self.track_length)

        # --- lap counter (counts up; never repeats a number) ---
        restart = False
        if self.last_lapnum is None:
            self.last_lapnum = lapnum
        else:
            wrapped = (self.prev_dist is not None and self.prev_dist > 0.9 * length
                       and 0 <= dist < 0.1 * length)
            if lapnum > self.last_lapnum or wrapped:
                if self.session_time - self.last_trigger_time > F1_LAP_DEBOUNCE_S:
                    self.lap_id += 1
                    self.last_trigger_time = self.session_time
                self.last_lapnum = max(self.last_lapnum, lapnum)
            elif lapnum < self.last_lapnum:      # flashback / restart
                restart = True
                self.last_lapnum = lapnum
        self.prev_dist = dist

        # --- g-forces: lateral -> X, vertical -> Y, longitudinal -> Z (like AC) ---
        if layout == "24":
            g_lat, g_long, g_vert = m[12] / 1000.0, m[13] / 1000.0, m[14] / 1000.0
        else:
            g_lat, g_long, g_vert = m[12], m[13], m[14]

        flag = 0
        if invalid:
            flag |= FLAG_INVALID
        if pit != 0 or driver_status in (2, 3):     # pitting / in-lap / out-lap
            flag |= FLAG_PIT

        # Only record while on track and past the start line
        live = dist >= 0 and driver_status != 0

        sample = (
            min(max(dist / length, 0.0), 1.0),
            cur_ms / 1000.0,
            m[0], m[1], m[2],
            m[3], m[4], m[5],
            float(speed),
            g_lat, g_vert, g_long,
            int(carpos),
            float(throttle), float(brake), float(steer),
            int(gear), int(rpm),
        )
        return Frame(
            live=live,
            lap=self.lap_id,
            sample=sample,
            car=f"f1_{year}",
            track=F1_TRACKS.get(self.track_id, f"track_{self.track_id}"),
            flag=flag,
            restart=restart,
        )

    def poll(self):
        data = self._recv()
        if data is None:
            return None
        return self.handle(data)


# ---------------------------------------------------------------------------
# Finding the game
# ---------------------------------------------------------------------------

def running_processes():
    """Lower-case names of all running programs (Windows)."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return set()
    return {row[0].lower() for row in csv.reader(out.splitlines()) if row}


def wait_for_source(requested, f1_port):
    """Block until a supported game is found. Returns a connected source."""
    f1 = None
    f1_available = requested in ("auto", "f1")
    announced = False

    while True:
        # Assetto Corsa / ACC: recognised by their program names
        if requested in ("auto", "ac", "acc"):
            procs = running_processes() if requested == "auto" else set()
            candidates = []
            if requested == "ac" or (requested == "auto" and procs & AC_PROCESSES):
                candidates.append("ac")
            if requested == "acc" or (requested == "auto" and procs & ACC_PROCESSES):
                candidates.append("acc")
            for game in candidates:
                source = SharedMemorySource(game)
                if source.connect():
                    if f1:
                        f1.close()
                    print(f"Detected {source.label}.")
                    return source

        # F1 25: recognised by UDP packets arriving
        if f1_available:
            if f1 is None:
                f1 = F1Source(f1_port)
                try:
                    f1.open()
                except OSError as e:
                    print(f"Could not listen on UDP port {f1_port} ({e}). Another program "
                          "(e.g. SimHub) may be using it. F1 detection is disabled.")
                    f1 = None
                    f1_available = False
            if f1 and f1.probe(1.0):
                print("Detected F1 25 (UDP telemetry is arriving).")
                return f1

        if not announced:
            print("Waiting for a game... (Assetto Corsa, ACC or F1 25). Press Ctrl+C to quit.")
            announced = True
        time.sleep(1.0)


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

def run_check(source, seconds=20):
    """Print live values so you can confirm they make sense."""
    for line in source.info():
        print(line)
    print(f"\nDrive (or sit on the grid) and watch these change, for about {seconds} seconds:\n")
    print(f"{'live':>4} {'lap':>3} {'lapTime':>8} {'trackPos':>8} {'speed':>6} "
          f"{'gas':>5} {'brake':>5} {'gear':>4} {'rpm':>5}   position (x, y, z)")
    end = time.time() + seconds
    last_print = 0.0
    got_any = False
    while time.time() < end:
        frame = source.poll()
        if frame is None or time.time() - last_print < 0.5:
            continue
        got_any = True
        last_print = time.time()
        s = frame.sample
        print(f"{'yes' if frame.live else 'no':>4} {frame.lap:>3} {s[S_TIME]:>8.2f} "
              f"{s[S_POS]:>8.4f} {s[S_SPEED]:>6.1f} {s[S_GAS]:>5.2f} {s[S_BRAKE]:>5.2f} "
              f"{s[S_GEAR]:>4} {s[S_RPM]:>5}   ({s[S_X]:.1f}, {s[S_Y]:.1f}, {s[S_Z]:.1f})")
    print()
    for line in source.summary():
        print(line)
    if not got_any:
        print("No data was received. See the Troubleshooting section of README.txt.")
    print("\nGear is shown as 1 = first, 0 = neutral, -1 = reverse.")


def run_logger(source, out_dir):
    session = None
    samples = []
    current_lap = None
    car = track = None
    flag = 0
    saved = 0
    next_lap_number = 0

    print("Recording. Drive complete laps; each one is saved as you cross the line.")
    print("Press Ctrl+C to stop.\n")

    def finish_lap():
        nonlocal session, saved, next_lap_number
        rows, info = process_lap(samples, next_lap_number, car, track, source.game, flag)
        if rows is None:
            print(f"  Lap skipped ({info['reason']})")
            return
        if session is None:
            session = CsvSession(out_dir, source.game, car, track)
        session.write(rows)
        notes = []
        if flag & FLAG_INVALID:
            notes.append("invalid lap")
        if flag & FLAG_PIT:
            notes.append("pit lap")
        extra = f"  [{', '.join(notes)}]" if notes else ""
        print(f"  Lap {next_lap_number}: saved  {fmt_time(info['lap_time'])}  "
              f"{info['length']:.0f} m  ({info['valid_bins']}/{info['bins']} valid bins){extra}")
        saved += 1
        next_lap_number += 1

    try:
        while True:
            frame = source.poll()
            if frame is None or not frame.live:
                continue

            # New car or track: start a fresh file
            if (frame.car, frame.track) != (car, track):
                if car is not None:
                    print("  Car or track changed: starting a new file.")
                car, track = frame.car, frame.track
                session, samples, current_lap, flag, next_lap_number = None, [], None, 0, 0
                print(f"Car: {car}   Track: {track}")

            if current_lap is None:
                current_lap = frame.lap
                samples, flag = [], 0
            elif frame.restart or frame.lap < current_lap:
                print("  Restart or flashback detected: discarding the unfinished lap.")
                current_lap = frame.lap
                samples, flag = [], 0
            elif frame.lap > current_lap:
                finish_lap()
                current_lap = frame.lap
                samples, flag = [], 0

            samples.append(frame.sample)
            flag |= frame.flag

    except KeyboardInterrupt:
        print("\nStopped.")

    if session:
        print(f"Saved {saved} lap(s) to: {session.path}")
        print("Upload that file on the RaceIQ page.")
    else:
        print("No completed laps were recorded, so no file was written.")


def main():
    parser = argparse.ArgumentParser(description="Record racing-game telemetry for RaceIQ.")
    parser.add_argument("--game", choices=["auto", "ac", "acc", "f1"], default="auto",
                        help="which game to record (default: auto-detect)")
    parser.add_argument("--out", default="recordings",
                        help="folder for the CSV files (default: recordings)")
    parser.add_argument("--check", action="store_true",
                        help="print live values for 20 seconds instead of recording")
    parser.add_argument("--f1-port", type=int, default=F1_DEFAULT_PORT,
                        help=f"UDP port F1 25 sends to (default: {F1_DEFAULT_PORT})")
    args = parser.parse_args()

    if os.name != "nt":
        sys.exit("This logger only works on Windows, where these games run.")

    source = None
    try:
        source = wait_for_source(args.game, args.f1_port)
        if args.check:
            run_check(source)
        else:
            run_logger(source, args.out)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if source:
            source.close()


if __name__ == "__main__":
    main()
