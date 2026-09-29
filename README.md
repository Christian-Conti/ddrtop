# ddrtop

`ddrtop` is an `htop`-like terminal monitor for real-time DDR bandwidth on **AMD/Xilinx Zynq UltraScale+ MPSoC** devices.

It directly accesses the PS **APMDDR (AXI Performance Monitor for DDR)** registers through `/dev/mem` and displays per-port read/write bandwidth using a `curses` interface.

## Features

- Real-time per-port read/write DDR bandwidth
- Monitoring of all six APMDDR slave ports
- Simultaneous RD/WR measurement for sampled ports
- Time-multiplexed sampling across ports
- Counter-delta measurements with 32-bit wrap handling
- Coherent 64-bit GCCR reads
- Automatic or fixed bandwidth scale
- Optional DDR peak utilization
- Configurable sampling interval
- No external Python packages required

## DDR Ports

| Port | Source |
|---:|---|
| 0 | RPU |
| 1 | CCI-0 |
| 2 | CCI-1 |
| 3 | HP0/DP |
| 4 | HP1/2 |
| 5 | HP3/DMA |

## How It Works

APMDDR provides **10 metric counters**. Since each port requires one counter for reads and one for writes, at most **five ports can be monitored simultaneously**.

`ddrtop` therefore rotates the omitted port between sampling windows while always measuring RD and WR traffic for a given port in the same window.

The interface reports:

```text
WINDOW exact
ALL~ cached
```

- `WINDOW exact`: exact, temporally coherent sum of the five ports sampled in the current window.
- `ALL~ cached`: estimated total across all six ports, using the previous measurement for the currently omitted port.

The reported `AGE` indicates how recently each port was measured.

## Requirements

- Zynq UltraScale+ MPSoC
- Linux
- Python 3
- `/dev/mem` access
- Root privileges
- `curses` terminal support

## Usage

```bash
chmod +x ddrtop.py
sudo ./ddrtop.py
```

or:

```bash
sudo python3 ddrtop.py
```

Press `q` to exit.

### Options

```bash
sudo ./ddrtop.py --help
```

Main options:

```text
--base ADDRESS          APMDDR base address (default: 0xFD0B0000)
--interval SECONDS      Sampling interval (default: 0.10)
--ddr-peak-gbps GBPS    Peak DDR bandwidth used for scaling/utilization
```

Example:

```bash
sudo ./ddrtop.py --interval 0.1 --ddr-peak-gbps 10.28
```

Without `--ddr-peak-gbps`, the bandwidth bars automatically scale to the observed traffic.

## Counter Handling

`ddrtop` computes bandwidth from counter deltas instead of resetting the metric counters after every sample.

For each window it:

1. programs the metric selectors;
2. reads the initial counters;
3. measures traffic for the configured interval;
4. reads the final counters;
5. computes the counter deltas.

32-bit byte-counter wrap-around is handled automatically.

The 64-bit GCCR counter is read using an **H-L-H** sequence to avoid inconsistent values during rollover.

When `--ddr-peak-gbps` is specified, `ddrtop` also checks that the sampling interval is short enough to avoid ambiguous 32-bit counter overflow.

## Notes

Because APMDDR has only ten metric counters, all six ports cannot be measured with simultaneous RD/WR counters in the same sampling interval.

Use `WINDOW exact` when temporal coherence is important. `ALL~ cached` provides an estimate of total DDR traffic across all six ports.
