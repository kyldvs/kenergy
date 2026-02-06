#!/usr/bin/env python3
"""Analyze a day's energy log and print a summary.

Usage:
    python3 analyze-power.py              # today
    python3 analyze-power.py 2026-02-06   # specific date
"""

import sys
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_DIR = Path.home() / "kenergy" / "logs"


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


NOMINAL_VOLTAGE = 11.4  # 3-cell Li-ion nominal voltage


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
        # Try with extra space for single-digit days
        try:
            return datetime.strptime(ts, "%a %b  %d %H:%M:%S %Y %z")
        except ValueError:
            return None


def estimate_sample_interval_h(samples: list[dict]) -> float | None:
    """Estimate the average interval between samples in hours."""
    timestamps = []
    for s in samples:
        dt = parse_ts(s["timestamp"])
        if dt:
            timestamps.append(dt)
    if len(timestamps) < 2:
        return None
    total_seconds = (timestamps[-1] - timestamps[0]).total_seconds()
    return total_seconds / (len(timestamps) - 1) / 3600


def fmt_mw(mw: float) -> str:
    if mw >= 1000:
        return f"{mw / 1000:.1f}W"
    return f"{mw:.0f}mW"


def print_header(title: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


def analyze(date_str: str) -> None:
    samples = load_samples(date_str)
    n = len(samples)

    print(f"\n  Energy Report: {date_str}")
    print(f"  {n} samples collected")

    # Time range
    if n > 0:
        print(f"  {samples[0]['timestamp']}  →  {samples[-1]['timestamp']}")

    # Battery
    cap = get_battery_capacity_wh()
    batteries = [s["battery_pct"] for s in samples if "battery_pct" in s]
    if len(batteries) >= 2:
        print_header("Battery")
        pct_start = batteries[0]
        pct_end = batteries[-1]
        pct_delta = pct_end - pct_start
        print(f"  {pct_start:.0f}% → {pct_end:.0f}%  ({pct_delta:+.0f}%)")

        if cap:
            current_wh, design_wh = cap
            health = current_wh / design_wh * 100
            wh_start = current_wh * pct_start / 100
            wh_end = current_wh * pct_end / 100
            wh_used = abs(wh_start - wh_end)
            print(f"  {wh_start:.1f} Wh → {wh_end:.1f} Wh  ({wh_used:.1f} Wh consumed)")
            print(f"  Capacity: {current_wh:.1f} Wh / {design_wh:.1f} Wh design  ({health:.0f}% health)")

    # Power draw
    interval_h = estimate_sample_interval_h(samples)

    cpu_vals = [s["power"]["cpu_mw"] for s in samples if "cpu_mw" in s.get("power", {})]
    gpu_vals = [s["power"]["gpu_mw"] for s in samples if "gpu_mw" in s.get("power", {})]
    combined = [s["power"]["combined_mw"] for s in samples if "combined_mw" in s.get("power", {})]

    if combined:
        print_header("Power Draw (avg / peak)")
        if cpu_vals:
            print(f"  CPU:      {fmt_mw(sum(cpu_vals) / len(cpu_vals)):>8}  / {fmt_mw(max(cpu_vals)):>8}")
        if gpu_vals:
            print(f"  GPU:      {fmt_mw(sum(gpu_vals) / len(gpu_vals)):>8}  / {fmt_mw(max(gpu_vals)):>8}")
        print(f"  Combined: {fmt_mw(sum(combined) / len(combined)):>8}  / {fmt_mw(max(combined)):>8}")

        if interval_h:
            total_wh = sum(c / 1000 * interval_h for c in combined)
            detail = f"  Total:    {total_wh:.1f} Wh over {len(combined)} samples"
            if cap:
                batt_pct = total_wh / cap[0] * 100
                detail += f"  (~{batt_pct:.0f}% of battery)"
            print(detail)
            if cap:
                avg_w = sum(combined) / len(combined) / 1000
                hrs_to_empty = cap[0] / avg_w
                print(f"  At avg draw, full battery lasts ~{hrs_to_empty:.1f}h")

    # Top processes by total energy impact
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
        print_header("Top Processes by Total Energy Impact")
        print(f"  {'Process':<35} {'Total':>8} {'Avg':>8} {'Peak':>8} {'Seen':>5}")
        ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:15]
        for name, total in ranked:
            avg = total / counts[name]
            print(f"  {name:<35} {total:>8.1f} {avg:>8.1f} {peaks[name]:>8.1f} {counts[name]:>5}")

    # Hourly power breakdown
    if combined:
        hourly: dict[str, list[float]] = defaultdict(list)
        for s in samples:
            ts = s["timestamp"]
            # Extract hour from timestamp like "Fri Feb  6 10:46:32 2026 -0800"
            parts = ts.split()
            if len(parts) >= 4:
                hour = parts[3].split(":")[0]
                if "combined_mw" in s.get("power", {}):
                    hourly[hour].append(s["power"]["combined_mw"])

        if hourly and interval_h:
            has_cap = cap is not None
            print_header("Hourly Combined Power")
            hdr = f"  {'Hour':>6}  {'Avg':>8}  {'Peak':>8}  {'~Wh':>6}"
            if has_cap:
                hdr += f"  {'~Batt%':>6}"
            hdr += f"  {'Samples':>8}"
            print(hdr)
            for hour in sorted(hourly.keys()):
                vals = hourly[hour]
                avg = sum(vals) / len(vals)
                wh = sum(v / 1000 * interval_h for v in vals)
                row = f"  {hour + ':00':>6}  {fmt_mw(avg):>8}  {fmt_mw(max(vals)):>8}  {wh:>5.1f}"
                if has_cap:
                    batt_pct = wh / cap[0] * 100
                    row += f"  {batt_pct:>5.1f}%"
                row += f"  {len(vals):>8}"
                print(row)

    print()


if __name__ == "__main__":
    date_str = sys.argv[1] if len(sys.argv) > 1 else __import__("datetime").date.today().isoformat()
    analyze(date_str)
