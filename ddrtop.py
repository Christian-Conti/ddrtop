#!/usr/bin/env python3
"""
ddrtop.py - htop-like DDR bandwidth monitor for Zynq UltraScale+ MPSoC PS APMDDR.

- Reads APMDDR via /dev/mem (root required)
- Shows all DDR slave ports 0..5 using time-multiplexed sampling
- Samples RD and WR for every selected port in the same sampling window
- Uses counter deltas instead of resetting counters at every sample
- Reads GCCR coherently as a 64-bit counter
- Separates the exact current-window total from the all-port cached estimate
- Stacked bars: RD (green) + WR (cyan)
- Optional utilization vs peak DDR bandwidth (--ddr-peak-gbps)
- Auto-scaling bars if peak is not provided

Keys:
  q  quit
"""

import argparse
import curses
import mmap
import os
import struct
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# ---- APMDDR base and register offsets ----
DEFAULT_BASE = 0xFD0B0000
MAP_SIZE     = 0x1000

OFF_GCCR_H   = 0x0000
OFF_GCCR_L   = 0x0004

OFF_MSR_0    = 0x0044
OFF_MSR_1    = 0x0048
OFF_MSR_2    = 0x004C

OFF_MCR_0    = 0x0100  # +0x10 per counter
OFF_CR       = 0x0300


# ---- Control register bits ----
CR_MET_CNT_EN  = 1 << 0
CR_MET_CNT_RST = 1 << 1
CR_GCCR_EN     = 1 << 16
CR_GCCR_RST    = 1 << 17


# ---- Metric select IDs ----
MET_WR_BYTE_CNT = 2
MET_RD_BYTE_CNT = 3


U32_MASK = 0xFFFFFFFF
U64_MASK = 0xFFFFFFFFFFFFFFFF


PORT_LABELS = {
    0: "RPU",
    1: "CCI-0",
    2: "CCI-1",
    3: "HP0/DP",
    4: "HP1/2",
    5: "HP3/DMA",
}


def _u32(x: int) -> int:
    return x & U32_MASK


def _delta_u32(cur: int, prev: int) -> int:
    return (cur - prev) & U32_MASK


def _delta_u64(cur: int, prev: int) -> int:
    return (cur - prev) & U64_MASK


def _delta_gccr(cur: int, prev: int) -> int:
    """
    Compute a GCCR delta.

    UG1087 exposes GCCR_H and GCCR_L. Some APM configurations can implement
    only the lower 32 bits, so when both upper halves are zero we also handle
    a possible 32-bit wrap correctly.
    """
    cur_hi = (cur >> 32) & U32_MASK
    prev_hi = (prev >> 32) & U32_MASK

    if cur_hi == 0 and prev_hi == 0:
        return _delta_u32(cur & U32_MASK, prev & U32_MASK)

    return _delta_u64(cur, prev)


def _fmt_rate(bps: float) -> str:
    if bps >= 1e9:
        return f"{bps/1e9:6.2f} GB/s"
    if bps >= 1e6:
        return f"{bps/1e6:6.1f} MB/s"
    if bps >= 1e3:
        return f"{bps/1e3:6.1f} kB/s"
    return f"{bps:6.0f}  B/s"


def _pct(v: float, full: float) -> float:
    if full <= 0:
        return 0.0
    return max(0.0, min(100.0, 100.0 * v / full))


def mcr_offset(i: int) -> int:
    return OFF_MCR_0 + (i * 0x10)


def _pack_sel(slot: int, metric: int) -> int:
    # [7:5] = slot, [4:0] = metric
    return ((slot & 0x7) << 5) | (metric & 0x1F)


class Mmio:
    def __init__(self, base_addr: int, size: int = MAP_SIZE):
        if os.geteuid() != 0:
            raise PermissionError("Run as root (sudo) to access /dev/mem.")

        self.base_addr = base_addr
        self.size = size

        page_size = mmap.PAGESIZE
        self.page_base = base_addr & ~(page_size - 1)
        self.page_off = base_addr - self.page_base

        self.fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        self.mm = mmap.mmap(
            self.fd,
            self.page_off + size,
            mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=self.page_base,
        )
        self.buf = memoryview(self.mm)

    def close(self) -> None:
        try:
            self.buf.release()
        except Exception:
            pass
        try:
            self.mm.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except Exception:
            pass

    def read32(self, off: int) -> int:
        i = self.page_off + off
        return struct.unpack_from("<I", self.buf, i)[0]

    def write32(self, off: int, val: int) -> None:
        i = self.page_off + off
        struct.pack_into("<I", self.buf, i, _u32(val))

    def read64_gccr(self) -> int:
        """
        Read GCCR coherently.

        GCCR_H and GCCR_L are separate 32-bit registers. Reading H-L-H avoids
        returning a torn 64-bit value if GCCR_L rolls over between accesses.
        """
        for _ in range(8):
            hi_1 = self.read32(OFF_GCCR_H)
            lo = self.read32(OFF_GCCR_L)
            hi_2 = self.read32(OFF_GCCR_H)

            if hi_1 == hi_2:
                return (hi_1 << 32) | lo

        # Extremely unlikely fallback if the high half keeps changing.
        hi = self.read32(OFF_GCCR_H)
        lo = self.read32(OFF_GCCR_L)
        return (hi << 32) | lo


def program_msr(mm: Mmio, counter_to_sel: Dict[int, Tuple[int, int]]) -> None:
    """
    Program counter selectors.

    counter_to_sel maps counter_index -> (DDR slot, metric).
    Each selected DDR port always gets one WR counter and one RD counter.
    """
    msr0 = 0
    msr1 = 0
    msr2 = 0

    for c in range(10):
        if c not in counter_to_sel:
            continue

        slot, metric = counter_to_sel[c]
        field = _pack_sel(slot, metric) & 0xFF

        if 0 <= c <= 3:
            msr0 |= field << (8 * c)
        elif 4 <= c <= 7:
            msr1 |= field << (8 * (c - 4))
        else:
            msr2 |= field << (8 * (c - 8))

    mm.write32(OFF_MSR_0, msr0)
    mm.write32(OFF_MSR_1, msr1)
    mm.write32(OFF_MSR_2, msr2)


def set_metric_counting(mm: Mmio, enabled: bool) -> None:
    """Enable or disable metric counters while keeping GCCR running."""
    cr = mm.read32(OFF_CR)

    # Never leave reset bits asserted.
    cr &= ~(CR_MET_CNT_RST | CR_GCCR_RST | CR_MET_CNT_EN)
    cr |= CR_GCCR_EN

    if enabled:
        cr |= CR_MET_CNT_EN

    mm.write32(OFF_CR, cr)


def apm_prepare(mm: Mmio) -> None:
    """
    Prepare APMDDR without resetting GCCR or metric counters.

    Counter baselines are read explicitly before every sampling window, so
    resetting the counters is unnecessary and would break delta accounting.
    """
    set_metric_counting(mm, False)


@dataclass
class PortState:
    rd_bps: float = 0.0
    wr_bps: float = 0.0
    last_update: float = 0.0
    sample_id: int = -1
    valid: bool = False


@dataclass
class SampleResult:
    rates: Dict[int, Tuple[float, float]]
    dt: float
    gccr_delta: int
    gccr_start: int
    gccr_end: int


class AutoScale:
    """Fixed scale or max-observed scale with slow decay."""

    def __init__(self, fixed_bps: Optional[float], decay_per_sec: float = 0.04):
        self.fixed = fixed_bps
        self.scale = fixed_bps if fixed_bps else 1.0
        self.decay_per_sec = decay_per_sec

    def update(self, observed_bps: float, dt: float) -> float:
        if self.fixed:
            self.scale = self.fixed
            return self.scale

        decay = max(0.0, min(0.5, self.decay_per_sec * dt))
        self.scale *= 1.0 - decay

        if observed_bps > self.scale:
            self.scale = observed_bps

        self.scale = max(self.scale, 1.0)
        return self.scale


def init_colors() -> Dict[str, int]:
    attrs = {
        "rd": 0,
        "wr": 0,
        "txt": 0,
        "hdr": curses.A_BOLD,
        "warn": 0,
        "hot": 0,
    }

    if not curses.has_colors():
        return attrs

    curses.start_color()
    try:
        curses.use_default_colors()
    except Exception:
        pass

    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_CYAN, -1)
    curses.init_pair(3, curses.COLOR_WHITE, -1)
    curses.init_pair(4, curses.COLOR_YELLOW, -1)
    curses.init_pair(5, curses.COLOR_RED, -1)

    attrs["rd"] = curses.color_pair(1)
    attrs["wr"] = curses.color_pair(2)
    attrs["txt"] = curses.color_pair(3)
    attrs["warn"] = curses.color_pair(4)
    attrs["hot"] = curses.color_pair(5)
    return attrs


def draw_stacked_bar(
    screen,
    y: int,
    x: int,
    width: int,
    rd: float,
    wr: float,
    scale: float,
    attrs: Dict[str, int],
) -> None:
    if width < 6:
        return

    inner = width - 2
    scale = max(scale, 1.0)

    rd_chars = int(inner * min(rd / scale, 1.0))
    wr_chars = int(inner * min(wr / scale, 1.0))

    if rd_chars + wr_chars > inner:
        wr_chars = max(0, inner - rd_chars)

    rem = inner - (rd_chars + wr_chars)

    try:
        screen.addstr(y, x, "[", attrs["txt"])
        screen.addstr(y, x + 1, " " * inner, attrs["txt"])

        if rd_chars:
            screen.addstr(
                y,
                x + 1,
                " " * rd_chars,
                attrs["rd"] | curses.A_REVERSE,
            )

        if wr_chars:
            screen.addstr(
                y,
                x + 1 + rd_chars,
                " " * wr_chars,
                attrs["wr"] | curses.A_REVERSE,
            )

        if rem:
            screen.addstr(
                y,
                x + 1 + rd_chars + wr_chars,
                " " * rem,
                attrs["txt"],
            )

        screen.addstr(y, x + width - 1, "]", attrs["txt"])
    except curses.error:
        pass


def ports_window(tick: int) -> List[int]:
    """
    APMDDR has 10 metric counters, therefore at most five DDR ports can be
    monitored with simultaneous RD+WR byte counters in one window.

    Rotate the omitted port so every port is sampled regularly. RD and WR for
    each included port are always measured in the same window.
    """
    missing = tick % 6
    return [p for p in range(6) if p != missing]


def sample_window(mm: Mmio, win_ports: List[int], interval_s: float) -> SampleResult:
    """
    Sample five DDR ports using paired WR/RD counters.

    The metric counters are not reset. Instead, a baseline and a final value
    are read and the 32-bit modular difference is used for each counter.
    """
    if len(win_ports) != 5:
        raise ValueError("win_ports must contain exactly 5 ports.")

    counter_map: Dict[int, Tuple[int, int]] = {}
    for i, p in enumerate(win_ports):
        counter_map[2 * i] = (p, MET_WR_BYTE_CNT)
        counter_map[2 * i + 1] = (p, MET_RD_BYTE_CNT)

    # Stop counting before changing selectors.
    set_metric_counting(mm, False)
    program_msr(mm, counter_map)

    # Establish a fresh baseline after the new selectors are active.
    start_counts = {
        c: mm.read32(mcr_offset(c))
        for c in range(10)
    }

    gccr_start = mm.read64_gccr()
    set_metric_counting(mm, True)
    t0 = time.monotonic()

    time.sleep(interval_s)

    t1 = time.monotonic()
    set_metric_counting(mm, False)
    gccr_end = mm.read64_gccr()

    end_counts = {
        c: mm.read32(mcr_offset(c))
        for c in range(10)
    }

    dt = max(1e-9, t1 - t0)
    gccr_delta = _delta_gccr(gccr_end, gccr_start)

    out: Dict[int, Tuple[float, float]] = {}
    for i, p in enumerate(win_ports):
        wr_bytes = _delta_u32(end_counts[2 * i], start_counts[2 * i])
        rd_bytes = _delta_u32(end_counts[2 * i + 1], start_counts[2 * i + 1])
        out[p] = (rd_bytes / dt, wr_bytes / dt)

    return SampleResult(
        rates=out,
        dt=dt,
        gccr_delta=gccr_delta,
        gccr_start=gccr_start,
        gccr_end=gccr_end,
    )


def run_ui(mm: Mmio, interval_s: float, peak_gbps: Optional[float]) -> None:
    fixed_scale_bps = peak_gbps * 1e9 if peak_gbps else None
    total_scale = AutoScale(fixed_scale_bps)
    port_scale = AutoScale(fixed_scale_bps)

    ports: Dict[int, PortState] = {
        p: PortState()
        for p in range(6)
    }

    apm_prepare(mm)

    def loop(screen) -> None:
        curses.curs_set(0)
        screen.nodelay(True)
        attrs = init_colors()

        tick = 0
        sample_id = 0
        start = time.monotonic()
        last_render = start

        while True:
            try:
                ch = screen.getch()
                if ch in (ord("q"), ord("Q")):
                    return
            except Exception:
                pass

            win = ports_window(tick)
            tick += 1
            sample_id += 1

            result = sample_window(mm, win, interval_s)
            now = time.monotonic()

            for p, (rd_bps, wr_bps) in result.rates.items():
                ports[p].rd_bps = rd_bps
                ports[p].wr_bps = wr_bps
                ports[p].last_update = now
                ports[p].sample_id = sample_id
                ports[p].valid = True

            # Exact total for counters measured in the same current window.
            window_rd = sum(result.rates[p][0] for p in win)
            window_wr = sum(result.rates[p][1] for p in win)
            window_bw = window_rd + window_wr

            # All-port value is an estimate because one port comes from an older window.
            cached_rd = sum(
                ports[p].rd_bps
                for p in range(6)
                if ports[p].valid
            )
            cached_wr = sum(
                ports[p].wr_bps
                for p in range(6)
                if ports[p].valid
            )
            cached_bw = cached_rd + cached_wr

            dt_render = max(1e-9, now - last_render)
            last_render = now

            apm_hz = result.gccr_delta / result.dt

            total_scale_bps = total_scale.update(cached_bw, dt_render)
            max_port_bw = max(
                (
                    ports[p].rd_bps + ports[p].wr_bps
                    for p in range(6)
                    if ports[p].valid
                ),
                default=0.0,
            )
            port_scale_bps = port_scale.update(max_port_bw, dt_render)

            screen.erase()
            h, w = screen.getmaxyx()

            elapsed = now - start
            missing = next(p for p in range(6) if p not in win)
            title = (
                f"ddrtop | APMDDR 0x{mm.base_addr:08X} | "
                f"dt={result.dt*1000:.1f}ms | t={elapsed:6.1f}s | "
                f"window={win} missing={missing} | q quit"
            )
            screen.addstr(0, 0, title[: max(0, w - 1)], attrs["hdr"])

            line1 = (
                f"WINDOW exact  RD {_fmt_rate(window_rd)}   "
                f"WR {_fmt_rate(window_wr)}   SUM {_fmt_rate(window_bw)}"
            )
            screen.addstr(1, 0, line1[: max(0, w - 1)], attrs["txt"])

            line2 = (
                f"ALL~ cached   RD {_fmt_rate(cached_rd)}   "
                f"WR {_fmt_rate(cached_wr)}   SUM {_fmt_rate(cached_bw)}"
            )
            screen.addstr(2, 0, line2[: max(0, w - 1)], attrs["txt"])

            bar_w = max(20, min(70, w - 2))

            if fixed_scale_bps:
                util = _pct(cached_bw, fixed_scale_bps)
                util_attr = attrs["txt"]
                if util >= 95.0:
                    util_attr = attrs["hot"]
                elif util >= 85.0:
                    util_attr = attrs["warn"]

                info = (
                    f"Peak={peak_gbps:.3f} GB/s  Util~={util:5.1f}%  "
                    f"(~ = one port may come from previous window)"
                )
                screen.addstr(3, 0, info[: max(0, w - 1)], util_attr | attrs["hdr"])
                draw_stacked_bar(
                    screen,
                    4,
                    0,
                    bar_w,
                    cached_rd,
                    cached_wr,
                    fixed_scale_bps,
                    attrs,
                )
            else:
                info = f"Scale=auto  total scale {_fmt_rate(total_scale_bps)}"
                screen.addstr(3, 0, info[: max(0, w - 1)], attrs["hdr"])
                draw_stacked_bar(
                    screen,
                    4,
                    0,
                    bar_w,
                    cached_rd,
                    cached_wr,
                    total_scale_bps,
                    attrs,
                )

            gccr_line = (
                f"GCCR delta={result.gccr_delta:>12d}   "
                f"APM clock est={apm_hz/1e6:8.2f} MHz   "
                f"RD=green WR=cyan"
            )
            screen.addstr(5, 0, gccr_line[: max(0, w - 1)], attrs["txt"])

            screen.addstr(
                7,
                0,
                "PORT  SOURCE    READ           WRITE          TOTAL          AGE     BAR"[: max(0, w - 1)],
                attrs["hdr"],
            )

            row = 8
            bar_col = 72
            bar_width = max(14, min(34, w - bar_col - 1))

            for p in range(6):
                if row >= h - 1:
                    break

                state = ports[p]

                if state.valid:
                    rd = state.rd_bps
                    wr = state.wr_bps
                    tot = rd + wr
                    age = now - state.last_update
                    age_text = f"{age:5.2f}s"
                else:
                    rd = 0.0
                    wr = 0.0
                    tot = 0.0
                    age = float("inf")
                    age_text = "  n/a "

                age_attr = attrs["txt"]
                if state.valid and age > 2.5 * interval_s:
                    age_attr = attrs["warn"]
                if state.valid and age > 5.0 * interval_s:
                    age_attr = attrs["hot"]

                left = (
                    f"{p:>4}  {PORT_LABELS[p]:<8}  "
                    f"{_fmt_rate(rd):>12}  {_fmt_rate(wr):>12}  "
                    f"{_fmt_rate(tot):>12}  {age_text}"
                )
                screen.addstr(
                    row,
                    0,
                    left[: min(max(0, w - 1), bar_col - 1)],
                    age_attr,
                )

                scale_for_port = fixed_scale_bps if fixed_scale_bps else port_scale_bps
                draw_stacked_bar(
                    screen,
                    row,
                    min(bar_col, max(0, w - 2)),
                    bar_width,
                    rd,
                    wr,
                    scale_for_port,
                    attrs,
                )

                row += 1

            screen.refresh()

    try:
        curses.wrapper(loop)
    finally:
        set_metric_counting(mm, False)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="ddrtop: htop-like DDR monitor via PS APMDDR (Zynq UltraScale+ MPSoC)"
    )
    ap.add_argument(
        "--base",
        default=f"0x{DEFAULT_BASE:X}",
        help="APMDDR base address (default 0xFD0B0000)",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=0.10,
        help="Sampling interval seconds (default 0.10)",
    )
    ap.add_argument(
        "--ddr-peak-gbps",
        type=float,
        default=None,
        help="Peak DDR bandwidth in GB/s for utilization meter",
    )
    args = ap.parse_args()

    if args.interval <= 0:
        ap.error("--interval must be > 0")

    if args.ddr_peak_gbps is not None and args.ddr_peak_gbps <= 0:
        ap.error("--ddr-peak-gbps must be > 0")

    if args.ddr_peak_gbps:
        wrap_time = (1 << 32) / (args.ddr_peak_gbps * 1e9)
        if args.interval >= 0.80 * wrap_time:
            ap.error(
                "--interval is too large for a 32-bit byte counter at the requested "
                f"peak bandwidth. Use < {0.80 * wrap_time:.3f} s."
            )

    base = int(args.base, 0)

    mm = Mmio(base_addr=base, size=MAP_SIZE)
    try:
        run_ui(mm, args.interval, args.ddr_peak_gbps)
    finally:
        mm.close()


if __name__ == "__main__":
    main()
