from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from urllib.parse import quote
import os

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import plotly.io as pio
from scipy.signal import find_peaks

pio.templates.default = "plotly_dark"

app = FastAPI(
    title="RaceIQ",
    version="4.0"
)

templates = Jinja2Templates(
    directory="app/templates"
)

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Tunable settings
# ---------------------------------------------------------------------------

DROP_LAST_LAP = False          # the final recorded lap is always cut short, so remove it entirely
COMPLETE_FRACTION = 0.95      # a lap must cover this much of the track to count as complete
FLYING_START_KMH = 30         # a lap starting slower than this is treated as an out-lap
CLOSURE_FRACTION = 0.02      # a complete lap must end within this share of track length of where it began
CLOSURE_MIN_M = 100           # ...but never stricter than this many metres
CORNER_MIN_PROMINENCE_KMH = 15  # minimum speed dip to count as a corner
CORNER_MIN_GAP_M = 80         # corners closer than this are merged
APEX_WINDOW_M = 100           # +/- window used to find the same corner on other laps
BRAKE_LOOKBACK_M = 500        # how far before the apex to look for the brake point
BRAKE_END_MAX_GAP_M = 200     # braking must end this close to the apex to belong to it
BRAKE_GAP_TOLERANCE_M = 10    # gaps shorter than this don't split a braking zone
EXIT_OFFSET_M = 100           # exit speed is measured this far after the apex
SHORT_SHIFT_FRACTION = 0.85   # upshifts below this share of max RPM are "short shifts"
NEAR_LIMIT_FRACTION = 0.98    # RPM above this share of max counts as "near the limit"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def find_col(df, candidates):
    """Return the first column name from candidates that exists in df."""
    for c in candidates:
        if c in df.columns:
            return c
    return None


def fmt(v, nd=1, signed=False, suffix=""):
    """Format a number for display; None/NaN becomes an em dash."""
    if v is None:
        return "—"
    try:
        if np.isnan(v):
            return "—"
    except TypeError:
        pass
    return f"{v:+.{nd}f}{suffix}" if signed else f"{v:.{nd}f}{suffix}"


def fmt_time(seconds):
    """Seconds -> m:ss.mmm"""
    if seconds is None or np.isnan(seconds):
        return "—"
    m = int(seconds // 60)
    s = seconds - 60 * m
    return f"{m}:{s:06.3f}"


def plot_html(fig):
    # Plotly.js is loaded once in index.html, so don't embed it in every chart
    return fig.to_html(full_html=False, include_plotlyjs=False)


def error_page(title, body_html, status_code=400):
    return HTMLResponse(
        f"<h1>{title}</h1>{body_html}"
        '<p><a href="/">Back</a></p>',
        status_code=status_code
    )


def pick_ground_axes(df):
    """Return the two position columns with the largest range (the ground plane).

    In your export Z is elevation, so this ends up choosing X and Y.
    """
    pos_cols = [
        c for c in ["world_position_x", "world_position_y", "world_position_z"]
        if c in df.columns
    ]
    if len(pos_cols) < 2:
        return None, None
    ranges = {c: df[c].max() - df[c].min() for c in pos_cols}
    top_two = sorted(ranges, key=ranges.get, reverse=True)[:2]
    return top_two[0], top_two[1]


# ---------------------------------------------------------------------------
# Data preparation
# ---------------------------------------------------------------------------

def prepare_dataframe(df):
    """Normalize column names and derive the channels the analysis needs.

    Adds: speed (km/h), lap, distance (m), t (seconds since lap start),
    accel_g (longitudinal acceleration in g).
    """
    df = df.copy()
    df.columns = df.columns.str.lower().str.strip()

    # Speed: derived from the velocity vector (m/s -> km/h)
    if "speed" not in df.columns:
        vel_cols = ["velocity_x", "velocity_y", "velocity_z"]
        if all(c in df.columns for c in vel_cols):
            df["speed"] = np.sqrt(
                df["velocity_x"] ** 2
                + df["velocity_y"] ** 2
                + df["velocity_z"] ** 2
            ) * 3.6

    # Number of bins per lap, measured before any filtering
    n_bins = None
    if "binindex" in df.columns:
        n_bins = int(df["binindex"].max()) + 1

    # Drop invalid bins (but never drop everything)
    if "validbin" in df.columns:
        valid = df[df["validbin"] == 1]
        if len(valid) > 0:
            df = valid

    # Lap column
    lap_col = find_col(df, ["lapnum", "lapindex", "lap"])
    df["lap"] = df[lap_col].astype(int) if lap_col else 1

    # The last lap in the file is the one the session ended on (in-lap, pit entry,
    # or a lap cut short), so it is never a usable lap. Remove it from everything.
    # Files made by race_logger.py (they have a 'game' column) only contain
    # completed laps, so nothing is removed from those.
    # Skipped if the file only has one lap, so there is always something to analyze.
    if DROP_LAST_LAP and "game" not in df.columns:
        lap_numbers = sorted(df["lap"].unique())
        if len(lap_numbers) > 1:
            df = df[df["lap"] != lap_numbers[-1]]

    if "binindex" in df.columns:
        df = df.drop_duplicates(subset=["lap", "binindex"])

    sort_cols = ["lap"] + (["binindex"] if "binindex" in df.columns else [])
    df = df.sort_values(sort_cols).reset_index(drop=True)

    # Distance around the lap (assumes evenly spaced bins)
    if n_bins and "tracklength" in df.columns:
        df["distance"] = df["binindex"] / n_bins * float(df["tracklength"].iloc[0])
    else:
        df["distance"] = df.groupby("lap").cumcount().astype(float)

    # Time since the start of each lap
    if "lap_time" in df.columns:
        df["t"] = df["lap_time"] - df.groupby("lap")["lap_time"].transform("first")
    else:
        df["t"] = np.nan

    # Longitudinal acceleration from speed over time (negative = braking)
    df["accel_g"] = np.nan
    if "speed" in df.columns and df["t"].notna().any():
        for lap, idx in df.groupby("lap").groups.items():
            g = df.loc[idx]
            t = g["t"].values
            v = g["speed"].values / 3.6
            if len(g) < 5:
                continue
            dt = np.gradient(t)
            dv = np.gradient(v)
            a = np.full(len(g), np.nan)
            ok = dt > 1e-4
            a[ok] = dv[ok] / dt[ok] / 9.81
            a = (
                pd.Series(a)
                .rolling(7, center=True, min_periods=1).median()
                .rolling(5, center=True, min_periods=1).mean()
                .values
            )
            df.loc[idx, "accel_g"] = a

    return df


# ---------------------------------------------------------------------------
# 1. Lap table and best-lap picker
# ---------------------------------------------------------------------------

def summarize_laps(df):
    """One row per lap with time, completeness and status.

    Returns (rows, best_lap, clean_laps).
    clean_laps = complete laps that start at speed (no out-lap).
    """
    if "binindex" in df.columns:
        n_bins = int(df["binindex"].max()) + 1
    else:
        n_bins = int(df.groupby("lap").size().max())

    x_col, y_col = pick_ground_axes(df)
    track_len = float(df["tracklength"].iloc[0]) if "tracklength" in df.columns else None
    closure_limit = max(CLOSURE_MIN_M, CLOSURE_FRACTION * track_len) if track_len else CLOSURE_MIN_M

    rows = []
    for lap, g in df.groupby("lap"):
        # Share of the track's bins that actually have a row (catches sparse laps)
        row_fraction = len(g) / n_bins
        # Share of the track spanned from first to last bin (catches short laps)
        if "binindex" in g.columns:
            span_fraction = (g["binindex"].max() - g["binindex"].min() + 1) / n_bins
        else:
            span_fraction = row_fraction

        # A real lap ends back near where it started (the start/finish line)
        closed = True
        if x_col:
            gap = float(np.hypot(
                g[x_col].iloc[-1] - g[x_col].iloc[0],
                g[y_col].iloc[-1] - g[y_col].iloc[0],
            ))
            closed = gap <= closure_limit

        lap_time = float(g["lap_time"].max()) if "lap_time" in g.columns else np.nan
        flag = int(g["lapflag"].max()) if "lapflag" in g.columns else 0
        complete = (
            span_fraction >= COMPLETE_FRACTION
            and row_fraction >= COMPLETE_FRACTION
            and closed
        )
        outlap = float(g["speed"].iloc[0]) < FLYING_START_KMH

        rows.append({
            "lap": int(lap),
            "time": lap_time,
            "time_str": fmt_time(lap_time) if complete else "—",
            "complete": bool(complete),
            "outlap": bool(outlap),
            "flag": flag,
            "top_speed": fmt(g["speed"].max(), 1),
            "avg_speed": fmt(g["speed"].mean(), 1),
            "bins": len(g),
        })

    clean = [r for r in rows
             if r["complete"] and not r["outlap"] and not r["flag"] and not np.isnan(r["time"])]
    complete_only = [r for r in rows if r["complete"] and not np.isnan(r["time"])]

    if clean:
        best = min(clean, key=lambda r: r["time"])
    elif complete_only:
        best = min(complete_only, key=lambda r: r["time"])
    else:
        best = max(rows, key=lambda r: r["bins"])
    best_lap = best["lap"]
    best_time = best["time"]

    for r in rows:
        if r["lap"] == best_lap:
            r["status"] = "Best"
        elif r["flag"] & 1:
            r["status"] = "Invalid"
        elif r["flag"] & 2:
            r["status"] = "Pit lap"
        elif r["outlap"]:
            r["status"] = "Out-lap"
        elif not r["complete"]:
            r["status"] = "Incomplete"
        else:
            r["status"] = ""
        if r["complete"] and not np.isnan(r["time"]) and not np.isnan(best_time):
            r["delta_str"] = "—" if r["lap"] == best_lap else fmt(r["time"] - best_time, 3, signed=True, suffix=" s")
        else:
            r["delta_str"] = "—"

    clean_laps = [r["lap"] for r in clean]
    return rows, best_lap, clean_laps


def parse_lap(value, laps, default):
    try:
        v = int(str(value).strip())
    except (ValueError, TypeError):
        return default
    return v if v in laps else default


# ---------------------------------------------------------------------------
# 2 + 3. Delta time and time-loss map
# ---------------------------------------------------------------------------

def compute_delta(lap_df, ref_df):
    """Running time difference (seconds, + = slower than the reference)
    and the time lost per 10 m (smoothed), both on lap_df's rows."""
    if lap_df["t"].isna().all() or ref_df["t"].isna().all():
        return None, None

    ref_t_at = np.interp(
        lap_df["distance"].values,
        ref_df["distance"].values,
        ref_df["t"].values
    )
    delta = lap_df["t"].values - ref_t_at

    dist = lap_df["distance"].values
    if len(dist) < 3 or np.any(np.diff(dist) <= 0):
        return delta, np.zeros(len(delta))

    slope_per_m = np.gradient(delta, dist)
    loss_per_10m = pd.Series(slope_per_m * 10).rolling(
        25, center=True, min_periods=1
    ).mean().values
    return delta, loss_per_10m


def build_delta_chart(lap_df, delta, lap, ref):
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=lap_df["distance"],
        y=delta,
        mode="lines",
        name=f"Lap {lap}",
        fill="tozeroy",
        line=dict(color="#e63946"),
        hovertemplate="%{x:.0f} m<br>%{y:+.3f} s<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dot", line_color="#888")
    fig.update_layout(
        title=f"Time delta: lap {lap} vs reference lap {ref}  (above 0 = slower)",
        xaxis_title="Distance (m)",
        yaxis_title="Delta (s)",
        height=420,
    )
    return plot_html(fig)


def build_loss_map(lap_df, loss, lap, ref, axes):
    x_col, y_col = axes
    if not x_col:
        return None

    lim = float(np.nanpercentile(np.abs(loss), 98)) if len(loss) else 0.0
    lim = max(lim, 0.005)

    fig = go.Figure()
    fig.add_trace(go.Scattergl(
        x=lap_df[x_col],
        y=lap_df[y_col],
        mode="markers",
        name="Time loss",
        marker=dict(
            size=5,
            color=loss,
            colorscale="RdYlGn",
            reversescale=True,   # red = losing time, green = gaining
            cmin=-lim,
            cmax=lim,
            colorbar=dict(title="s lost<br>per 10 m", len=0.75, thickness=15),
        ),
        hovertemplate="%{marker.color:+.3f} s per 10 m<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=[lap_df[x_col].iloc[0]],
        y=[lap_df[y_col].iloc[0]],
        mode="markers",
        name="Start",
        marker=dict(size=14, color="yellow", symbol="star"),
    ))
    fig.update_layout(
        title=dict(text=f"Where lap {lap} gains / loses time vs lap {ref}", x=0.0),
        height=700,
        margin=dict(t=80, b=80, l=20, r=20),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, scaleanchor="x", scaleratio=1),
        legend=dict(orientation="h", x=0.5, xanchor="center", y=-0.02, yanchor="top"),
    )
    return plot_html(fig)


# ---------------------------------------------------------------------------
# 4. Corner detection and comparison
# ---------------------------------------------------------------------------

def detect_corners(ref_df):
    """Corners = speed minima on the reference lap, numbered in driving order."""
    spd = ref_df["speed"].rolling(5, center=True, min_periods=1).mean().values
    span = float(spd.max() - spd.min())
    prominence = max(CORNER_MIN_PROMINENCE_KMH, 0.08 * span)

    bin_m = float(np.median(np.diff(ref_df["distance"].values))) if len(ref_df) > 1 else 1.0
    bin_m = bin_m if bin_m > 0 else 1.0
    min_gap = max(3, int(CORNER_MIN_GAP_M / bin_m))

    idx, _ = find_peaks(-spd, prominence=prominence, distance=min_gap)
    return [
        {"num": i + 1, "apex_dist": float(ref_df["distance"].iloc[k])}
        for i, k in enumerate(idx)
    ]


def measure_corner(g, apex_d, brake_thr):
    """Brake point, minimum speed, exit speed for the corner near apex_d."""
    d = g["distance"].values
    spd = g["speed"].values
    brk = g["brake"].values

    window = np.where((d >= apex_d - APEX_WINDOW_M) & (d <= apex_d + APEX_WINDOW_M))[0]
    if len(window) == 0:
        return None

    ai = int(window[np.argmin(spd[window])])
    apex_dist = float(d[ai])

    brake_dist = None
    entry_speed = None
    lo = int(np.searchsorted(d, apex_dist - BRAKE_LOOKBACK_M))
    seg = brk[lo:ai + 1] > brake_thr
    if seg.any():
        last = int(np.where(seg)[0][-1])
        if apex_dist - d[lo + last] <= BRAKE_END_MAX_GAP_M:
            # Walk back to the start of the braking zone, bridging short gaps
            # so a brief dip in the brake trace doesn't cut the zone short.
            step_m = float(np.median(np.diff(d))) if len(d) > 1 else 1.0
            step_m = step_m if step_m > 0 else 1.0
            tolerance = max(1, int(BRAKE_GAP_TOLERANCE_M / step_m))
            first = last
            gap = 0
            i = last - 1
            while i >= 0:
                if seg[i]:
                    first = i
                    gap = 0
                else:
                    gap += 1
                    if gap > tolerance:
                        break
                i -= 1
            bi = lo + first
            brake_dist = float(d[bi])
            entry_speed = float(spd[bi])

    exit_speed = float(np.interp(apex_dist + EXIT_OFFSET_M, d, spd))

    return {
        "idx": ai,
        "apex_dist": apex_dist,
        "min_speed": float(spd[ai]),
        "brake_dist": brake_dist,
        "entry_speed": entry_speed,
        "exit_speed": exit_speed,
    }


def delta_class(v, good_when_positive=True, tol=0.5):
    if v is None or np.isnan(v):
        return ""
    if abs(v) < tol:
        return ""
    return "good" if (v > 0) == good_when_positive else "bad"


def corner_comparison(lap_df, ref_df, corners, brake_thr):
    rows = []
    points = []   # for numbering the corners on the track map
    for c in corners:
        rm = measure_corner(ref_df, c["apex_dist"], brake_thr)
        lm = measure_corner(lap_df, c["apex_dist"], brake_thr)
        if lm is not None:
            points.append({"num": c["num"], "idx": lm["idx"], "speed": lm["min_speed"]})
        if rm is None or lm is None:
            continue

        d_min = lm["min_speed"] - rm["min_speed"]
        d_exit = lm["exit_speed"] - rm["exit_speed"]
        d_brake = None
        if lm["brake_dist"] is not None and rm["brake_dist"] is not None:
            d_brake = lm["brake_dist"] - rm["brake_dist"]

        rows.append({
            "num": c["num"],
            "dist": fmt(c["apex_dist"], 0),
            "ref_brake": fmt(rm["brake_dist"], 0),
            "ref_min": fmt(rm["min_speed"], 1),
            "ref_exit": fmt(rm["exit_speed"], 1),
            "lap_brake": fmt(lm["brake_dist"], 0),
            "lap_min": fmt(lm["min_speed"], 1),
            "lap_exit": fmt(lm["exit_speed"], 1),
            "d_brake": fmt(d_brake, 0, signed=True, suffix=" m"),
            "d_min": fmt(d_min, 1, signed=True),
            "d_exit": fmt(d_exit, 1, signed=True),
            "cls_min": delta_class(d_min),
            "cls_exit": delta_class(d_exit),
        })
    return rows, points


# ---------------------------------------------------------------------------
# 5. Driving style metrics
# ---------------------------------------------------------------------------

def driving_metrics(g):
    """Share of lap distance spent in each driving state, plus smoothness."""
    thr = g["throttle"].values.astype(float)
    brk = g["brake"].values.astype(float)
    n = len(g)

    tmax = thr.max() if thr.max() > 0 else 1.0
    bmax = brk.max()

    full = thr >= 0.98 * tmax
    if bmax > 0:
        braking = brk >= 0.05 * bmax
        coasting = (thr <= 0.02 * tmax) & (brk <= 0.02 * bmax)
    else:
        braking = np.zeros(n, dtype=bool)
        coasting = thr <= 0.02 * tmax

    trail = None
    if "steering" in g.columns and braking.any():
        steer = np.abs(g["steering"].values.astype(float))
        smax = steer.max()
        if smax > 0:
            trail = float(np.mean((braking & (steer > 0.1 * smax))[braking])) * 100

    def smoothness(x, scale):
        return float(np.mean(np.abs(np.diff(x))) / scale * 100) if len(x) > 1 else np.nan

    return {
        "full": float(np.mean(full)) * 100,
        "braking": float(np.mean(braking)) * 100,
        "coasting": float(np.mean(coasting)) * 100,
        "partial": max(0.0, float(100 - np.mean(full) * 100 - np.mean(braking) * 100 - np.mean(coasting) * 100)),
        "trail": trail,
        "thr_smooth": smoothness(thr, tmax),
        "brk_smooth": smoothness(brk, bmax if bmax > 0 else 1.0),
    }


def style_rows(lap_m, ref_m):
    def row(label, key, nd=1, suffix="%"):
        a, b = lap_m.get(key), ref_m.get(key)
        return {"label": label, "lap": fmt(a, nd, suffix=suffix), "ref": fmt(b, nd, suffix=suffix)}

    return [
        row("Full throttle (of lap distance)", "full"),
        row("Partial throttle", "partial"),
        row("Braking", "braking"),
        row("Coasting (no pedals)", "coasting"),
        row("Trail braking (share of braking with steering)", "trail"),
        row("Throttle roughness (lower = smoother)", "thr_smooth", 2, ""),
        row("Brake roughness (lower = smoother)", "brk_smooth", 2, ""),
    ]


# ---------------------------------------------------------------------------
# 6. Consistency across laps
# ---------------------------------------------------------------------------

def consistency_analysis(lap_groups, clean_laps, corners, brake_thr):
    """Corner-by-corner spread across all clean laps."""
    if len(clean_laps) < 2 or not corners:
        return None

    recs = []
    for lap in clean_laps:
        g = lap_groups[lap]
        for c in corners:
            m = measure_corner(g, c["apex_dist"], brake_thr)
            if m:
                recs.append({
                    "lap": lap,
                    "corner": f"C{c['num']}",
                    "num": c["num"],
                    "brake_dist": m["brake_dist"],
                    "min_speed": m["min_speed"],
                    "exit_speed": m["exit_speed"],
                })
    if not recs:
        return None

    cdf = pd.DataFrame(recs)
    order = [f"C{c['num']}" for c in corners]

    table = []
    for corner, grp in cdf.groupby("corner", sort=False):
        table.append({
            "corner": corner,
            "num": int(grp["num"].iloc[0]),
            "n": len(grp),
            "brake_std": grp["brake_dist"].std() if grp["brake_dist"].notna().sum() > 1 else np.nan,
            "min_std": grp["min_speed"].std(),
            "exit_std": grp["exit_speed"].std(),
        })
    table.sort(key=lambda r: r["num"])

    worst = None
    valid = [r for r in table if not np.isnan(r["min_std"])]
    if valid:
        w = max(valid, key=lambda r: r["min_std"])
        worst = f"{w['corner']} (apex speed varies by about {w['min_std']:.1f} km/h)"

    for r in table:
        r["brake_std_str"] = fmt(r["brake_std"], 1, suffix=" m")
        r["min_std_str"] = fmt(r["min_std"], 1, suffix=" km/h")
        r["exit_std_str"] = fmt(r["exit_std"], 1, suffix=" km/h")

    cdf["lap_label"] = cdf["lap"].astype(str)

    fig_speed = px.box(
        cdf, x="corner", y="min_speed", points="all", hover_data=["lap_label"],
        category_orders={"corner": order},
        title="Apex speed by corner (each dot is a lap)",
        labels={"min_speed": "Min speed (km/h)", "corner": ""},
    )
    fig_speed.update_layout(height=400)

    cdf["brake_dev"] = cdf["brake_dist"] - cdf.groupby("corner")["brake_dist"].transform("median")
    fig_brake = px.box(
        cdf.dropna(subset=["brake_dev"]), x="corner", y="brake_dev", points="all",
        hover_data=["lap_label"],
        category_orders={"corner": order},
        title="Braking point vs corner median (+ = braked later, m)",
        labels={"brake_dev": "Brake point deviation (m)", "corner": ""},
    )
    fig_brake.update_layout(height=400)

    return {
        "table": table,
        "worst": worst,
        "speed_chart": plot_html(fig_speed),
        "brake_chart": plot_html(fig_brake),
        "n_laps": len(clean_laps),
    }


# ---------------------------------------------------------------------------
# 7. Gear and shift analysis
# ---------------------------------------------------------------------------

def gear_analysis(df, use_laps, lap_df, axes):
    if "gear" not in df.columns:
        return None

    gdf = df[df["lap"].isin(use_laps)] if use_laps else df
    max_rpm = float(df["rpm"].max())

    shifts = []
    for lap, g in gdf.groupby("lap"):
        gear = g["gear"].values
        rpm = g["rpm"].values
        dist = g["distance"].values
        for i in np.where(np.diff(gear) == 1)[0]:
            if gear[i] >= 1:
                shifts.append({
                    "lap": int(lap),
                    "shift": f"{int(gear[i])}→{int(gear[i + 1])}",
                    "from_gear": int(gear[i]),
                    "rpm": float(rpm[i]),
                    "distance": float(dist[i]),
                })

    result = {
        "max_rpm": f"{max_rpm:.0f}",
        "short_limit": f"{SHORT_SHIFT_FRACTION * max_rpm:.0f}",
        "table": [],
        "hist": None,
        "usage": None,
        "map": None,
        "near_limit_pct": fmt(float(np.mean(gdf["rpm"] >= NEAR_LIMIT_FRACTION * max_rpm)) * 100, 1, suffix="%"),
    }

    if shifts:
        sdf = pd.DataFrame(shifts)
        for shift, grp in sdf.groupby("shift"):
            result["table"].append({
                "shift": shift,
                "from_gear": int(grp["from_gear"].iloc[0]),
                "count": len(grp),
                "avg": f"{grp['rpm'].mean():.0f}",
                "min": f"{grp['rpm'].min():.0f}",
                "max": f"{grp['rpm'].max():.0f}",
                "short": int((grp["rpm"] < SHORT_SHIFT_FRACTION * max_rpm).sum()),
            })
        result["table"].sort(key=lambda r: r["from_gear"])

        order = [r["shift"] for r in result["table"]]
        fig = px.histogram(
            sdf, x="rpm", color="shift", nbins=40, barmode="overlay", opacity=0.75,
            category_orders={"shift": order},
            title="Upshift RPM by gear change",
            labels={"rpm": "RPM at shift"},
        )
        fig.add_vline(
            x=SHORT_SHIFT_FRACTION * max_rpm, line_dash="dot", line_color="#ffb703",
            annotation_text="short-shift limit", annotation_position="top left",
        )
        fig.update_layout(height=420)
        result["hist"] = plot_html(fig)

    usage = (
        gdf["gear"].value_counts(normalize=True).sort_index() * 100
    ).reset_index()
    usage.columns = ["gear", "share"]
    usage["gear"] = usage["gear"].astype(str)
    fig_u = px.bar(
        usage, x="gear", y="share",
        title="Share of lap distance spent in each gear",
        labels={"share": "% of distance", "gear": "Gear"},
    )
    fig_u.update_layout(height=380)
    result["usage"] = plot_html(fig_u)

    x_col, y_col = axes
    if x_col:
        gm = lap_df.copy()
        gm["gear_label"] = gm["gear"].astype(str)
        gear_order = [str(g) for g in sorted(gm["gear"].unique())]
        fig_m = px.scatter(
            gm, x=x_col, y=y_col, color="gear_label",
            category_orders={"gear_label": gear_order},
            title="Gear map (selected lap)",
            labels={"gear_label": "Gear"},
        )
        fig_m.update_traces(marker=dict(size=5))
        fig_m.update_layout(
            height=650,
            xaxis=dict(visible=False),
            yaxis=dict(visible=False, scaleanchor="x", scaleratio=1),
        )
        result["map"] = plot_html(fig_m)

    return result


# ---------------------------------------------------------------------------
# Track map (selected lap) and the remaining charts
# ---------------------------------------------------------------------------

def build_track_map(lap_df, corner_points, axes):
    """Overhead 2D map colored by a selectable channel, with overlays."""
    x_col, y_col = axes
    if not x_col:
        return None

    fig = go.Figure()

    hover_cols = [c for c in ["speed", "throttle", "brake", "rpm", "gear"] if c in lap_df.columns]
    fig.add_trace(go.Scattergl(
        x=lap_df[x_col],
        y=lap_df[y_col],
        mode="markers",
        name="Racing line",
        marker=dict(
            size=5,
            color=lap_df["speed"],
            colorscale="Turbo",
            colorbar=dict(title="speed", len=0.75, thickness=15),
        ),
        customdata=lap_df[hover_cols],
        hovertemplate=(
            "Speed: %{customdata[0]:.1f} km/h<br>"
            "Throttle: %{customdata[1]:.2f}<br>"
            "Brake: %{customdata[2]:.2f}<br>"
            "RPM: %{customdata[3]:.0f}<extra></extra>"
        ),
    ))

    if lap_df["brake"].max() > 0:
        braking = lap_df[lap_df["brake"] > 0.5 * lap_df["brake"].max()]
        fig.add_trace(go.Scattergl(
            x=braking[x_col], y=braking[y_col],
            mode="markers", name="Heavy braking",
            marker=dict(size=8, color="red", symbol="square"),
            visible="legendonly",
        ))

    if lap_df["throttle"].max() > 0:
        full_thr = lap_df[lap_df["throttle"] > 0.98 * lap_df["throttle"].max()]
        fig.add_trace(go.Scattergl(
            x=full_thr[x_col], y=full_thr[y_col],
            mode="markers", name="Full throttle",
            marker=dict(size=6, color="lime"),
            visible="legendonly",
        ))

    if corner_points:
        pts = lap_df.iloc[[p["idx"] for p in corner_points]]
        fig.add_trace(go.Scatter(
            x=pts[x_col], y=pts[y_col],
            mode="markers+text", name="Corners",
            text=[f"C{p['num']}<br>{p['speed']:.0f}" for p in corner_points],
            textposition="top center",
            marker=dict(size=11, color="white", symbol="diamond",
                        line=dict(color="black", width=1)),
        ))

    fig.add_trace(go.Scatter(
        x=[lap_df[x_col].iloc[0]], y=[lap_df[y_col].iloc[0]],
        mode="markers", name="Start",
        marker=dict(size=14, color="yellow", symbol="star"),
    ))

    channels = [
        c for c in ["speed", "throttle", "brake", "rpm", "gear", "steering", "accel_g"]
        if c in lap_df.columns
    ]
    buttons = [
        dict(
            label=m,
            method="restyle",
            args=[
                {"marker.color": [lap_df[m].fillna(0).tolist()],
                 "marker.colorbar.title.text": m},
                [0],
            ],
        )
        for m in channels
    ]

    fig.update_layout(
        title=dict(text="Track Map", x=0.0, y=0.97),
        height=750,
        margin=dict(t=110, b=80, l=20, r=20),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False, scaleanchor="x", scaleratio=1),
        legend=dict(orientation="h", x=0.5, xanchor="center", y=-0.02, yanchor="top"),
        updatemenus=[dict(buttons=buttons, x=0.0, xanchor="left",
                          y=1.0, yanchor="bottom", direction="down")],
    )
    return plot_html(fig)


def build_overlay_charts(df, overlay_laps, lap_df, ref_df, lap, ref):
    x_title = "Distance (m)"

    plot_df = df[df["lap"].isin(overlay_laps)].copy()
    plot_df["lap"] = plot_df["lap"].astype(str)
    lap_order = [str(l) for l in overlay_laps]

    speed_fig = px.line(
        plot_df, x="distance", y="speed", color="lap",
        category_orders={"lap": lap_order},
        title="Speed profile (km/h)", labels={"distance": x_title},
    )
    speed_fig.update_layout(height=450)

    rpm_fig = px.line(
        plot_df, x="distance", y="rpm", color="lap",
        category_orders={"lap": lap_order},
        title="RPM trace", labels={"distance": x_title},
    )
    rpm_fig.update_layout(height=450)

    tb_fig = px.line(
        lap_df, x="distance", y=["throttle", "brake"],
        title=f"Throttle vs brake (lap {lap})", labels={"distance": x_title},
    )
    tb_fig.update_layout(height=420)

    charts = {
        "speed": plot_html(speed_fig),
        "rpm": plot_html(rpm_fig),
        "tb": plot_html(tb_fig),
        "steer": None,
        "accel": None,
    }

    if "steering" in lap_df.columns:
        steer_fig = px.line(
            lap_df, x="distance", y="steering",
            title=f"Steering (lap {lap} - where positive is right and negative is left)", labels={"distance": x_title},
        )
        steer_fig.update_layout(height=380)
        charts["steer"] = plot_html(steer_fig)

    if lap_df["accel_g"].notna().any():
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=lap_df["distance"], y=lap_df["accel_g"],
            mode="lines", name=f"Lap {lap}", line=dict(color="#e63946"),
        ))
        if ref != lap:
            fig.add_trace(go.Scatter(
                x=ref_df["distance"], y=ref_df["accel_g"],
                mode="lines", name=f"Ref lap {ref}",
                line=dict(color="#8ecae6", dash="dot"),
            ))
        fig.add_hline(y=0, line_color="#666")
        fig.update_layout(
            title="Longitudinal acceleration (g, negative = braking)",
            xaxis_title=x_title, yaxis_title="g", height=420,
        )
        charts["accel"] = plot_html(fig)

    return charts


# ---------------------------------------------------------------------------
# Putting it all together
# ---------------------------------------------------------------------------

def build_context(df, filename, lap_arg, ref_arg):
    lap_rows, best_lap, clean_laps = summarize_laps(df)
    laps = [r["lap"] for r in lap_rows]

    lap = parse_lap(lap_arg, laps, best_lap)
    ref = parse_lap(ref_arg, laps, best_lap)

    axes = pick_ground_axes(df)   # same two ground axes for every lap
    lap_groups = {l: g.reset_index(drop=True) for l, g in df.groupby("lap")}
    lap_df = lap_groups[lap]
    ref_df = lap_groups[ref]

    bmax = float(df["brake"].max())
    brake_thr = 0.1 * bmax if bmax > 0 else 0.05

    # Which laps to overlay on the speed / RPM charts
    overlay_laps = [r["lap"] for r in lap_rows if r["complete"]] or laps
    if lap not in overlay_laps:
        overlay_laps = sorted(overlay_laps + [lap])

    # 4. corners (numbered on the reference lap)
    corners = detect_corners(ref_df)
    corner_rows, corner_points = corner_comparison(lap_df, ref_df, corners, brake_thr)

    # 2 + 3. delta and time loss
    delta, loss = compute_delta(lap_df, ref_df)
    delta_chart = loss_map = None
    if delta is not None and lap != ref:
        delta_chart = build_delta_chart(lap_df, delta, lap, ref)
        loss_map = build_loss_map(lap_df, loss, lap, ref, axes)

    # 5. driving style
    style = style_rows(driving_metrics(lap_df), driving_metrics(ref_df))

    # 6. consistency
    consistency = consistency_analysis(lap_groups, clean_laps, corners, brake_thr)

    # 7. gears
    gears = gear_analysis(df, clean_laps, lap_df, axes)

    # Charts
    charts = build_overlay_charts(df, overlay_laps, lap_df, ref_df, lap, ref)
    charts["track"] = build_track_map(lap_df, corner_points, axes)

    best_row = next(r for r in lap_rows if r["lap"] == best_lap)
    stats = {
        "file": filename,
        "track": str(df["trackid"].iloc[0]) if "trackid" in df.columns else "n/a",
        "car": str(df["carid"].iloc[0]) if "carid" in df.columns else "n/a",
        "track_length": (
            round(float(df["tracklength"].iloc[0]), 1)
            if "tracklength" in df.columns else None
        ),
        "laps": len(laps),
        "clean_laps": len(clean_laps),
        "best_lap": best_lap,
        "best_time": best_row["time_str"],
        "rows": len(df),
        "max_speed": round(float(df["speed"].max()), 1),
        "max_rpm": round(float(df["rpm"].max())),
        "corners": len(corners),
    }

    lap_options = []
    for r in lap_rows:
        tag = f" ({r['status']})" if r["status"] else ""
        lap_options.append({"value": r["lap"], "label": f"Lap {r['lap']} – {r['time_str']}{tag}"})

    return {
        "file": filename,
        "lap": lap,
        "ref": ref,
        "stats": stats,
        "lap_rows": lap_rows,
        "lap_options": lap_options,
        "charts": charts,
        "delta_chart": delta_chart,
        "loss_map": loss_map,
        "same_lap": lap == ref,
        "corner_rows": corner_rows,
        "style_rows": style,
        "consistency": consistency,
        "gears": gears,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"r": None}
    )


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    print("UPLOAD RECEIVED:", file.filename)

    name = os.path.basename(file.filename or "")
    if not name.lower().endswith(".csv"):
        return error_page("Upload Error", "<p>Please upload a .csv file.</p>")

    path = os.path.join(UPLOAD_DIR, name)
    with open(path, "wb") as f:
        f.write(await file.read())

    # Redirect to the analysis page so lap pickers can re-use the saved file
    return RedirectResponse(url=f"/analyze?file={quote(name)}", status_code=303)


@app.get("/analyze", response_class=HTMLResponse)
async def analyze(request: Request, file: str, lap: str = "", ref: str = ""):
    name = os.path.basename(file)
    path = os.path.join(UPLOAD_DIR, name)

    if not os.path.isfile(path):
        return error_page("Not found", f"<p>No uploaded file named {name}.</p>", 404)

    try:
        df = pd.read_csv(path)
    except Exception as e:
        return error_page("CSV Error", f"<p>Could not read the file: {e}</p>")

    df = prepare_dataframe(df)

    required_columns = ["speed", "throttle", "brake", "rpm"]
    missing = [c for c in required_columns if c not in df.columns]
    if missing:
        return error_page(
            "CSV Error",
            "<p>Missing columns (speed is derived from velocity_X/Y/Z):</p>"
            "<ul>" + "".join(f"<li>{c}</li>" for c in missing) + "</ul>"
            f"<p>Columns found: {', '.join(df.columns)}</p>"
        )

    ctx = build_context(df, name, lap, ref)

    return templates.TemplateResponse(
        request,
        "index.html",
        {"r": ctx}
    )
