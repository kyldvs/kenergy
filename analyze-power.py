#!/usr/bin/env python3
"""Analyze energy logs and print a summary.

Usage:
    python3 analyze-power.py              # last 2 hours (default)
    python3 analyze-power.py -4hrs        # last 4 hours
    python3 analyze-power.py 2026-02-06   # specific date
"""

import sys
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path.home() / "kenergy" / "logs"

# ── ANSI helpers ────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def style(text: str, *codes: str) -> str:
    if not _USE_COLOR or not codes:
        return str(text)
    return "".join(codes) + str(text) + RESET


def visible_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


# ── Formatting helpers ──────────────────────────────────────────────


def fmt_mw(mw: float) -> str:
    if mw >= 1000:
        return f"{mw / 1000:.1f}W"
    return f"{mw:.0f}mW"


def fmt_duration(hours: float) -> str:
    minutes = hours * 60
    if minutes < 60:
        return f"{minutes:.1f} min"
    h = int(minutes // 60)
    m = int(minutes % 60)
    return f"{h}h {m}m"


def _pad_cell(cell: str, width: int, align: str) -> str:
    """Pad a (possibly styled) cell string to exact visible width."""
    gap = width - visible_len(cell)
    if align == ">":
        return " " + " " * gap + cell + " "
    return " " + cell + " " * gap + " "


def print_box(title: str, lines: list[str]) -> None:
    """Print free-form content in a bordered box with a title."""
    inner = max(
        *(visible_len(line) + 2 for line in lines),
        visible_len(title) + 4,
    )
    title_vis = visible_len(title)
    top = "┌─ " + style(title, BOLD, CYAN) + " " + "─" * (inner - title_vis - 3) + "┐"
    bot = "└" + "─" * inner + "┘"
    print(f"\n  {top}")
    for line in lines:
        gap = inner - visible_len(line) - 2
        print(f"  │ {line}{' ' * gap} │")
    print(f"  {bot}")


def print_table(
    title: str,
    columns: list[tuple[str, str, int]],
    rows: list[list[str]],
) -> None:
    """Print a bordered table with title, headers, and vertical bars.

    columns: list of (header, align, min_width) where align is '<' or '>'.
    rows:    list of lists of (possibly styled) cell strings.
    """
    widths = [max(len(hdr), min_w) for hdr, _, min_w in columns]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], visible_len(cell))

    # Padded column widths: " content " = width + 2
    pw = [w + 2 for w in widths]

    # Ensure first column is wide enough for the title
    title_vis = visible_len(title)
    min_first = title_vis + 3  # "─ Title "
    if pw[0] < min_first:
        widths[0] += min_first - pw[0]
        pw[0] = min_first

    def h_line(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * w for w in pw) + right

    # Top border with embedded title
    top = "┌─ " + style(title, BOLD, CYAN) + " " + "─" * (pw[0] - title_vis - 3)
    for i in range(1, len(pw)):
        top += "┬" + "─" * pw[i]
    top += "┐"

    # Header row
    hdr = "│"
    for i, (name, align, _) in enumerate(columns):
        hdr += _pad_cell(style(name, DIM), widths[i], align) + "│"

    sep = h_line("├", "┼", "┤")
    bot = h_line("└", "┴", "┘")

    print(f"\n  {top}")
    print(f"  {hdr}")
    print(f"  {sep}")
    for row in rows:
        line = "│"
        for i in range(len(columns)):
            cell = row[i] if i < len(row) else ""
            line += _pad_cell(cell, widths[i], columns[i][1]) + "│"
        print(f"  {line}")
    print(f"  {bot}")


# ── Data loading ────────────────────────────────────────────────────

NOMINAL_VOLTAGE = 11.4  # 3-cell Li-ion nominal voltage


def load_samples(date_str: str) -> list[dict]:
    path = LOG_DIR / f"{date_str}.jsonl"
    if not path.exists():
        print(f"No log file found: {path}")
        sys.exit(1)
    samples = []
    for line in path.read_text().splitlines():
        if line.strip():
            samples.append(json.loads(line))
    return samples


def load_recent_samples(hours: float) -> list[dict]:
    """Load samples from the last N hours across date boundaries."""
    now = datetime.now().astimezone()
    cutoff = now - timedelta(hours=hours)

    dates_to_check: list[str] = []
    d = cutoff.date()
    while d <= now.date():
        dates_to_check.append(d.isoformat())
        d += timedelta(days=1)

    samples = []
    for date_str in dates_to_check:
        path = LOG_DIR / f"{date_str}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            if line.strip():
                s = json.loads(line)
                ts = parse_ts(s.get("timestamp", ""))
                if ts and ts >= cutoff:
                    samples.append(s)

    if not samples:
        print(f"No samples found in the last {hours:g} hours.")
        sys.exit(1)
    return samples


def get_battery_capacity_wh() -> tuple[float, float] | None:
    """Query ioreg for battery capacity. Returns (current_wh, design_wh) or None."""
    try:
        out = subprocess.check_output(
            ["ioreg", "-r", "-c", "AppleSmartBattery", "-w0"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return None

    nominal_mah = None
    design_mah = None
    for line in out.splitlines():
        m = re.search(r'"NominalChargeCapacity"\s*=\s*(\d+)', line)
        if m:
            nominal_mah = int(m.group(1))
        m = re.search(r'"DesignCapacity"\s*=\s*(\d+)', line)
        if m:
            design_mah = int(m.group(1))

    if nominal_mah and design_mah:
        return (
            nominal_mah * NOMINAL_VOLTAGE / 1000,
            design_mah * NOMINAL_VOLTAGE / 1000,
        )
    return None


def parse_ts(ts: str) -> datetime | None:
    """Parse timestamp like 'Fri Feb  6 10:46:32 2026 -0800'."""
    try:
        return datetime.strptime(ts, "%a %b %d %H:%M:%S %Y %z")
    except ValueError:
        try:
            return datetime.strptime(ts, "%a %b  %d %H:%M:%S %Y %z")
        except ValueError:
            return None


def compute_duration_h(samples: list[dict]) -> float | None:
    """Compute total collection duration in hours from first to last sample."""
    timestamps = []
    for s in samples:
        dt = parse_ts(s["timestamp"])
        if dt:
            timestamps.append(dt)
    if len(timestamps) < 2:
        return None
    return (timestamps[-1] - timestamps[0]).total_seconds() / 3600


# ── Main analysis ───────────────────────────────────────────────────


def analyze(samples: list[dict], label: str) -> None:
    n = len(samples)
    duration_h = compute_duration_h(samples)

    # Title
    print()
    print(f"  {style('Energy Report', BOLD)}  {label}")
    detail = f"{n} samples"
    if duration_h:
        detail += f" over {fmt_duration(duration_h)}"
    print(f"  {style(detail, DIM)}")

    if n > 0:
        ts_start = parse_ts(samples[0]["timestamp"])
        ts_end = parse_ts(samples[-1]["timestamp"])
        if ts_start and ts_end:
            print(f"  {style(ts_start.strftime('%H:%M:%S'), DIM)} {style('→', DIM)} {style(ts_end.strftime('%H:%M:%S'), DIM)}")

    # ── Battery ─────────────────────────────────────────────────

    cap = get_battery_capacity_wh()
    batteries = [s["battery_pct"] for s in samples if "battery_pct" in s]
    if len(batteries) >= 2:
        pct_start = batteries[0]
        pct_end = batteries[-1]
        pct_delta = pct_end - pct_start
        delta_color = GREEN if pct_delta > 0 else (YELLOW if pct_delta < 0 else DIM)

        # Walk consecutive readings to accumulate total charged / discharged
        total_charged_pct = 0.0
        total_drained_pct = 0.0
        for prev, cur in zip(batteries, batteries[1:]):
            diff = cur - prev
            if diff > 0:
                total_charged_pct += diff
            elif diff < 0:
                total_drained_pct += -diff

        box_lines = [
            f"{pct_start:.0f}% \u2192 {pct_end:.0f}%  ({style(f'{pct_delta:+.0f}%', delta_color)})",
        ]

        # Show total charged / drained when there's activity
        cycle_parts = []
        if total_charged_pct > 0:
            cycle_parts.append(style(f"+{total_charged_pct:.0f}% charged", GREEN))
        if total_drained_pct > 0:
            cycle_parts.append(style(f"-{total_drained_pct:.0f}% drained", YELLOW))
        if cycle_parts:
            box_lines.append("  ".join(cycle_parts))

        if cap:
            current_wh, design_wh = cap
            health = current_wh / design_wh * 100
            wh_charged = current_wh * total_charged_pct / 100
            wh_drained = current_wh * total_drained_pct / 100
            wh_parts = []
            if wh_charged > 0:
                wh_parts.append(style(f"+{wh_charged:.1f} Wh charged", GREEN))
            if wh_drained > 0:
                wh_parts.append(style(f"-{wh_drained:.1f} Wh drained", YELLOW))
            if wh_parts:
                box_lines.append("  ".join(wh_parts))
            health_color = GREEN if health >= 80 else (YELLOW if health >= 60 else RED)
            box_lines.append(
                f"Capacity: {current_wh:.1f} / {design_wh:.1f} Wh design  ({style(f'{health:.0f}% health', health_color)})"
            )
        print_box("Battery", box_lines)

    # ── Power Draw ──────────────────────────────────────────────

    cpu_vals = [s["power"]["cpu_mw"] for s in samples if "cpu_mw" in s.get("power", {})]
    gpu_vals = [s["power"]["gpu_mw"] for s in samples if "gpu_mw" in s.get("power", {})]
    combined = [s["power"]["combined_mw"] for s in samples if "combined_mw" in s.get("power", {})]

    if combined:
        power_rows = []
        if cpu_vals:
            power_rows.append([
                "CPU",
                fmt_mw(sum(cpu_vals) / len(cpu_vals)),
                fmt_mw(max(cpu_vals)),
            ])
        if gpu_vals:
            power_rows.append([
                "GPU",
                fmt_mw(sum(gpu_vals) / len(gpu_vals)),
                fmt_mw(max(gpu_vals)),
            ])
        power_rows.append([
            style("Combined", BOLD),
            style(fmt_mw(sum(combined) / len(combined)), BOLD),
            style(fmt_mw(max(combined)), BOLD),
        ])
        print_table(
            "Power Draw",
            [("", "<", 10), ("Avg", ">", 8), ("Peak", ">", 8)],
            power_rows,
        )

        if batteries and len(batteries) >= 2 and cap and duration_h and duration_h > 0:
            if total_drained_pct > 0:
                actual_wh = cap[0] * total_drained_pct / 100
                drain_rate_w = actual_wh / duration_h
                hrs_to_empty = cap[0] / drain_rate_w
                print()
                print(f"  {style(f'{actual_wh:.1f} Wh', BOLD, YELLOW)} drained in {fmt_duration(duration_h)}  ({total_drained_pct:.0f}% battery)")
                print(f"  At this rate, full battery lasts {style(f'~{hrs_to_empty:.1f}h', BOLD)}")

    # ── Top Processes ───────────────────────────────────────────

    totals: dict[str, float] = defaultdict(float)
    peaks: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)

    for s in samples:
        for p in s.get("processes", []):
            name = p["name"]
            totals[name] += p["energy_impact"]
            peaks[name] = max(peaks[name], p["energy_impact"])
            counts[name] += 1

    if totals:
        ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:15]
        top_total = ranked[0][1] if ranked else 1
        proc_rows = []
        for name, total in ranked:
            avg = total / counts[name]
            # Color intensity by relative energy impact
            if total > top_total * 0.5:
                name_styled = style(name, BOLD, RED)
            elif total > top_total * 0.2:
                name_styled = style(name, YELLOW)
            else:
                name_styled = name
            proc_rows.append([
                name_styled,
                f"{total:.0f}",
                f"{avg:.1f}",
                f"{peaks[name]:.1f}",
                str(counts[name]),
            ])
        print_table(
            "Top Processes",
            [("Process", "<", 30), ("Total", ">", 8), ("Avg", ">", 8), ("Peak", ">", 8), ("Seen", ">", 5)],
            proc_rows,
        )

    # ── Hourly Power ────────────────────────────────────────────

    if combined:
        hourly: dict[str, list[float]] = defaultdict(list)
        hourly_battery: dict[str, tuple[float, float]] = {}
        for s in samples:
            dt = parse_ts(s["timestamp"])
            if not dt:
                continue
            hour = f"{dt.hour:02d}"
            if "combined_mw" in s.get("power", {}):
                hourly[hour].append(s["power"]["combined_mw"])
            if "battery_pct" in s:
                pct = s["battery_pct"]
                if hour not in hourly_battery:
                    hourly_battery[hour] = (pct, pct)
                else:
                    hourly_battery[hour] = (hourly_battery[hour][0], pct)

        if hourly:
            draining = batteries and len(batteries) >= 2 and batteries[0] > batteries[-1]
            has_battery = bool(hourly_battery) and cap is not None and draining

            cols: list[tuple[str, str, int]] = [
                ("Hour", "<", 6),
                ("Avg", ">", 8),
                ("Peak", ">", 8),
            ]
            if has_battery:
                cols += [("~Wh", ">", 6), ("~Batt%", ">", 6)]
            cols.append(("Samples", ">", 7))

            hour_rows = []
            for hour in sorted(hourly.keys()):
                vals = hourly[hour]
                avg = sum(vals) / len(vals)
                row = [
                    hour + ":00",
                    fmt_mw(avg),
                    fmt_mw(max(vals)),
                ]
                if has_battery:
                    if hour in hourly_battery:
                        first_pct, last_pct = hourly_battery[hour]
                        pct_used = first_pct - last_pct
                        wh = cap[0] * pct_used / 100
                    else:
                        wh = 0.0
                        pct_used = 0.0
                    row += [f"{wh:.1f}", f"{pct_used:.1f}%"]
                row.append(str(len(vals)))
                hour_rows.append(row)
            print_table("Hourly Power", cols, hour_rows)

    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        # Default: last 2 hours
        samples = load_recent_samples(2.0)
        analyze(samples, "Last 2 hours")
    else:
        arg = sys.argv[1]
        # -Xhrs or -Xhr pattern (e.g. -3hrs, -1.5hr)
        m = re.match(r"^-?(\d+(?:\.\d+)?)hrs?$", arg)
        if m:
            hours = float(m.group(1))
            samples = load_recent_samples(hours)
            label = f"Last {hours:g} hour{'s' if hours != 1 else ''}"
            analyze(samples, label)
        # YYYY-MM-DD date
        elif re.match(r"^\d{4}-\d{2}-\d{2}$", arg):
            samples = load_samples(arg)
            analyze(samples, arg)
        else:
            print(f"Invalid argument: {arg}")
            print("Usage: kenergy analyze [-Xhrs | YYYY-MM-DD]")
            sys.exit(1)
