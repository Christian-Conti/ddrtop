
    
  
            h, w = screen.getmaxyx()

            elapsed = now - start
            missing = next(p for p in range(6) if p not in win)
            title = f"ddrtop | APMDDR 0x{mm.base_addr:08X} | dt={result.dt*1000:.1f}ms | t={elapsed:6.1f}s | window={win} missing={missing} | q quit"
            screen.addstr(0, 0, title[: max(0, w - 1)], attrs["hdr"])

            line1 = f"WINDOW exact  RD {_fmt_rate(window_rd)}   WR {_fmt_rate(window_wr)}   SUM {_fmt_rate(window_bw)}"
            screen.addstr(1, 0, line1[: max(0, w - 1)], attrs["txt"])

            line2 = f"ALL~ cached   RD {_fmt_rate(cached_rd)}   WR {_fmt_rate(cached_wr)}   SUM {_fmt_rate(cached_bw)}"
            screen.addstr(2, 0, line2[: max(0, w - 1)], attrs["txt"])

            bar_w = max(20, min(70, w - 2))

            if fixed_scale_bps:
                util = _pct(cached_bw, fixed_scale_bps)
                util_attr = attrs["txt"]
                if util >= 95.0:
                    util_attr = attrs["hot"]
                elif util >= 85.0:
                    util_attr = attrs["warn"]

                info = f"Peak={peak_gbps:.3f} GB/s  Util~={util:5.1f}%  (~ = one port may come from previous window)"
                screen.addstr(3, 0, info[: max(0, w - 1)], util_attr | attrs["hdr"])
                draw_stacked_bar(screen, 4, 0, bar_w, cached_rd, cached_wr, fixed_scale_bps, attrs)
            else:
                info = f"Scale=auto  total scale {_fmt_rate(total_scale_bps)}"
                screen.addstr(3, 0, info[: max(0, w - 1)], attrs["hdr"])
                draw_stacked_bar(screen, 4, 0, bar_w, cached_rd, cached_wr, total_scale_bps, attrs)

            gccr_line = f"GCCR delta={result.gccr_delta:>12d}   APM clock est={apm_hz/1e6:8.2f} MHz   RD=green WR=cyan"
            screen.addstr(5, 0, gccr_line[: max(0, w - 1)], attrs["txt"])

            screen.addstr(7, 0, "PORT  SOURCE    READ           WRITE          TOTAL          AGE     BAR"[: max(0, w - 1)], attrs["hdr"])

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

                left = f"{p:>4}  {PORT_LABELS[p]:<8}  {_fmt_rate(rd):>12}  {_fmt_rate(wr):>12}  {_fmt_rate(tot):>12}  {age_text}"
                screen.addstr(row, 0, left[: min(max(0, w - 1), bar_col - 1)], age_attr)

                scale_for_port = fixed_scale_bps if fixed_scale_bps else port_scale_bps
                draw_stacked_bar(screen, row, min(bar_col, max(0, w - 2)), bar_width, rd, wr, scale_for_port, attrs)

                row += 1

            screen.refresh()

    try:
        curses.wrapper(loop)
    finally:
        set_metric_counting(mm, False)


def main() -> None:
    ap = argparse.ArgumentParser(description="ddrtop: htop-like DDR monitor via PS APMDDR (Zynq UltraScale+ MPSoC)")
    ap.add_argument("--base", default=f"0x{DEFAULT_BASE:X}", help="APMDDR base address (default 0xFD0B0000)")
    ap.add_argument("--interval", type=float, default=0.10, help="Sampling interval seconds (default 0.10)")
    ap.add_argument("--ddr-peak-gbps", type=float, default=None, help="Peak DDR bandwidth in GB/s for utilization meter")
    args = ap.parse_args()

    if args.interval <= 0:
        ap.error("--interval must be > 0")

    if args.ddr_peak_gbps is not None and args.ddr_peak_gbps <= 0:
        ap.error("--ddr-peak-gbps must be > 0")

    if args.ddr_peak_gbps:
        wrap_time = (1 << 32) / (args.ddr_peak_gbps * 1e9)
        if args.interval >= 0.80 * wrap_time:
            ap.error(f"--interval is too large for a 32-bit byte counter at the requested peak bandwidth. Use < {0.80 * wrap_time:.3f} s.")

    base = int(args.base, 0)

    mm = Mmio(base_addr=base, size=MAP_SIZE)
    try:
        run_ui(mm, args.interval, args.ddr_peak_gbps)
    finally:
        mm.close()


if __name__ == "__main__":
    main()
