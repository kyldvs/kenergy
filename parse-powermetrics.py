#!/usr/bin/env python3
"""Parse `sudo powermetrics` output into JSONL records.

Writes to ~/energy/logs/YYYY-MM-DD.jsonl based on the current date.
Automatically rolls over to a new file at midnight.

Usage:
    sudo powermetrics --show-process-energy -i 5000 | python3 powermetrics-to-jsonl.py
    sudo powermetrics --show-process-energy -i 5000 | python3 powermetrics-to-jsonl.py --top 20
"""

import sys
import json
import re
import argparse
import signal
import time
from datetime import date, datetime
from pathlib import Path

LOG_DIR = Path.home() / "kenergy" / "logs"

# ── ANSI helpers ────────────────────────────────────────────────────

_USE_COLOR = sys.stdout.isatty()

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
BRIGHT_BLACK = "\033[90m"


def style(text: str, *codes: str) -> str:
    if not _USE_COLOR or not codes:
        return str(text)
    return "".join(codes) + str(text) + RESET


def parse_timestamp(line: str) -> str | None:
    m = re.search(
        r"\*\*\* Sampled system activity \((.+?)\) \((\S+) elapsed\)", line
    )
    if not m:
        return None
    return m.group(1).strip()


def parse_tasks(lines: list[str], top_n: int) -> list[dict]:
    tasks: list[dict] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 9:
            continue
        # Name may contain spaces — PID is always an integer or -1/-2
        # Work backwards from the known numeric fields
        # Format: Name  ID  CPU_ms/s  User%  DL<2  DL2-5  WkIntr  WkPkg  Energy
        try:
            energy = float(parts[-1])
            pkg_idle = float(parts[-2])
            wk_intr = float(parts[-3])
            dl_2_5 = float(parts[-4])
            dl_lt2 = float(parts[-5])
            user_pct = float(parts[-6])
            cpu_ms = float(parts[-7])
            pid = int(parts[-8])
        except (ValueError, IndexError):
            continue

        name = " ".join(parts[:-8])
        if pid in (-1, -2):
            continue

        tasks.append({
            "name": name,
            "pid": pid,
            "cpu_ms_per_s": cpu_ms,
            "user_pct": user_pct,
            "deadlines_lt2ms": dl_lt2,
            "deadlines_2_5ms": dl_2_5,
            "wakeups_intr": wk_intr,
            "wakeups_pkg_idle": pkg_idle,
            "energy_impact": energy,
        })

    tasks.sort(key=lambda t: t["energy_impact"], reverse=True)
    if top_n > 0:
        tasks = tasks[:top_n]
    return tasks


def parse_kv(lines: list[str], prefix: str) -> float | None:
    for line in lines:
        if prefix in line:
            m = re.search(r"[\d.]+", line.split(prefix)[-1])
            if m:
                return float(m.group())
    return None


def parse_network(lines: list[str]) -> dict:
    net: dict = {}
    for line in lines:
        m = re.match(r"\s*(out|in):\s+([\d.]+)\s+packets/s,\s+([\d.]+)\s+(\S+)", line)
        if m:
            direction = m.group(1)
            net[f"{direction}_packets_per_s"] = float(m.group(2))
            net[f"{direction}_bytes_per_s"] = float(m.group(3))
    return net


def parse_disk(lines: list[str]) -> dict:
    disk: dict = {}
    for line in lines:
        m = re.match(
            r"\s*(read|write):\s+([\d.]+)\s+ops/s\s+([\d.]+)\s+KBytes/s", line
        )
        if m:
            op = m.group(1)
            disk[f"{op}_ops_per_s"] = float(m.group(2))
            disk[f"{op}_kb_per_s"] = float(m.group(3))
    return disk


def parse_power(lines: list[str]) -> dict:
    power: dict = {}
    for line in lines:
        m = re.match(r"\s*(CPU|GPU|ANE|Combined)[^:]*:\s+([\d.]+)\s+mW", line)
        if m:
            key = m.group(1).lower()
            power[f"{key}_mw"] = float(m.group(2))
    return power


def flush_sample(raw_lines: list[str], top_n: int) -> dict | None:
    text = "\n".join(raw_lines)

    ts = None
    for line in raw_lines:
        ts = parse_timestamp(line)
        if ts:
            break
    if not ts:
        return None

    # Extract sections
    task_lines: list[str] = []
    in_tasks = False
    all_lines = raw_lines

    for line in all_lines:
        if "*** Running tasks ***" in line:
            in_tasks = True
            continue
        if in_tasks:
            if line.startswith("****") or line.startswith("***"):
                in_tasks = False
                continue
            if line.strip() and not line.strip().startswith("Name"):
                task_lines.append(line)

    record: dict = {
        "timestamp": ts,
        "processes": parse_tasks(task_lines, top_n),
    }

    battery = parse_kv(raw_lines, "percent_charge:")
    if battery is not None:
        record["battery_pct"] = battery

    record["network"] = parse_network(raw_lines)
    record["disk"] = parse_disk(raw_lines)
    record["power"] = parse_power(raw_lines)

    gpu_residency = parse_kv(raw_lines, "GPU HW active residency:")
    if gpu_residency is not None:
        record["gpu_active_pct"] = gpu_residency

    return record


def open_log_file(current_date: date) -> tuple[date, object]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{current_date.isoformat()}.jsonl"
    return current_date, open(path, "a")


def fmt_ts(raw_ts: str) -> str:
    """Format 'Fri Feb  6 12:15:26 2026 -0800' → '2026-02-06 12:15:26 PM -0800'."""
    for fmt in ("%a %b %d %H:%M:%S %Y %z", "%a %b  %d %H:%M:%S %Y %z"):
        try:
            dt = datetime.strptime(raw_ts, fmt)
            tz = dt.strftime("%z")  # e.g. "-0800"
            return dt.strftime(f"%Y-%m-%d %I:%M:%S %p {tz}")
        except ValueError:
            continue
    return raw_ts


def fmt_now() -> str:
    """Format current local time in the same style."""
    dt = datetime.now().astimezone()
    tz = dt.strftime("%z")
    return dt.strftime(f"%Y-%m-%d %I:%M:%S %p {tz}")


def write_record(record: dict, today: date, f, start: float, count: int) -> tuple[date, object]:
    now = date.today()
    if now != today:
        f.close()
        today, f = open_log_file(now)
    f.write(json.dumps(record) + "\n")
    f.flush()
    if count == 1:
        print(
            f"\n  {style('Started watching', BOLD)} @ {fmt_now()}\n",
            flush=True,
        )
    n = len(record.get("processes", []))
    ts = style(f"[{fmt_ts(record['timestamp'])}]", BRIGHT_BLACK)
    sample = style(f"(sample {count})", CYAN)
    print(
        f"  {ts} {sample} {n} processes \u2192 {f.name}",
        file=sys.stdout,
        flush=True,
    )
    return today, f


def main() -> None:
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--top", type=int, default=0,
        help="Only include the top N processes by energy impact (0 = all)",
    )
    args = parser.parse_args()

    today, f = open_log_file(date.today())
    buf: list[str] = []
    sample_sep = re.compile(r"^\*\*\* Sampled system activity")
    count = 0

    try:
        t0 = time.monotonic()
        for line in sys.stdin:
            line = line.rstrip("\n")
            if "underflow" in line:
                continue
            if sample_sep.match(line) and buf:
                record = flush_sample(buf, args.top)
                if record:
                    count += 1
                    today, f = write_record(record, today, f, t0, count)
                buf = []
                t0 = time.monotonic()
            buf.append(line)

        if buf:
            record = flush_sample(buf, args.top)
            if record:
                count += 1
                today, f = write_record(record, today, f, t0, count)
    finally:
        f.close()
        if count:
            print(f"\n  {style('Done.', BOLD)} {count} samples collected.", flush=True)


if __name__ == "__main__":
    main()
