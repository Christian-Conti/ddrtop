# ddrtop

`ddrtop` is an `htop`-like terminal monitor for real-time DDR bandwidth on **AMD/Xilinx Zynq UltraScale+ MPSoC** devices.

It directly accesses the PS **APMDDR (AXI Performance Monitor for DDR)** registers through `/dev/mem` and displays per-port read/write bandwidth using an interactive `curses` interface.

## Features

- Real-time DDR bandwidth monitoring
- Per-port **read**, **write**, and **total** bandwidth
- Monitors all six DDR slave ports
- Simultaneous read/write measurement for every sampled port
- Time-multiplexed sampling across DDR ports
- Counter-delta based measurements without resetting counters at every sample
- Coherent 64-bit GCCR reads
- Estimated APM clock frequency
- Stacked terminal bars for read/write traffic
- Automatic bandwidth scale
- Optional DDR peak bandwidth and utilization display
- Configurable sampling interval
- No external Python packages required

## Supported DDR Ports

`ddrtop` monitors the six APMDDR slave slots:

| Port | Source |
|---:|---|
| 0 | RPU |
| 1 | CCI-0 |
| 2 | CCI-1 |
| 3 | HP0/DP |
| 4 | HP1/2 |
| 5 | HP3/DMA |

The displayed names correspond to the APMDDR slot mapping used by the script.

## How It Works

The Zynq UltraScale+ APMDDR exposes **10 metric counters**.

To measure both read and write bandwidth for a port, `ddrtop` assigns two counters:

```text
Port N
 ├── WR_BYTE_CNT
 └── RD_BYTE_CNT
```

This means that at most **five DDR ports can be measured simultaneously**.

To cover all six ports, `ddrtop` rotates the omitted port between sampling windows:

```text
Window 0: ports 1 2 3 4 5
Window 1: ports 0 2 3 4 5
Window 2: ports 0 1 3 4 5
...
```

Read and write counters for a given port are always measured during the **same sampling window**.

### Exact vs. Cached Bandwidth

The interface reports two aggregate values:

```text
WINDOW exact
ALL~ cached
```

`WINDOW exact` is the exact sum of the five ports sampled during the current measurement window.

`ALL~ cached` includes all six ports, but one port contains the value obtained during its previous sampling window.

Therefore:

```text
WINDOW exact = temporally coherent measurement of 5 ports
ALL~ cached  = estimate of the total traffic across all 6 ports
```

The age displayed next to each port indicates how recently that value was measured.

## Requirements

- AMD/Xilinx **Zynq UltraScale+ MPSoC**
- Linux
- Python 3
- Access to `/dev/mem`
- Root privileges
- Terminal with `curses` support

No additional Python modules are required.

## Usage

Make the script executable:

```bash
chmod +x ddrtop.py
```

Run it as root:

```bash
sudo ./ddrtop.py
```

or:

```bash
sudo python3 ddrtop.py
```

Press:

```text
q
```

to exit.

## Command-Line Options

```bash
sudo ./ddrtop.py --help
```

Available options:

```text
--base ADDRESS
```

APMDDR base address.

Default:

```text
0xFD0B0000
```

Example:

```bash
sudo ./ddrtop.py --base 0xFD0B0000
```

---

```text
--interval SECONDS
```

Sampling interval in seconds.

Default:

```text
0.10
```

Example:

```bash
sudo ./ddrtop.py --interval 0.25
```

---

```text
--ddr-peak-gbps GBPS
```

Specify the peak DDR bandwidth in GB/s.

When this option is provided, `ddrtop` uses the value as the fixed scale for the bandwidth bars and displays estimated DDR utilization.

Example:

```bash
sudo ./ddrtop.py --ddr-peak-gbps 10.28
```

Without this option, the bandwidth bars automatically scale according to the observed traffic.

## Example

```bash
sudo ./ddrtop.py \
    --interval 0.1 \
    --ddr-peak-gbps 10.28
```

The interface reports information similar to:

```text
ddrtop | APMDDR 0xFD0B0000 | dt=100.1ms | t=12.4s | window=[0,1,2,3,4] missing=5

WINDOW exact  RD   3.42 GB/s   WR   1.18 GB/s   SUM   4.60 GB/s
ALL~ cached   RD   3.55 GB/s   WR   1.24 GB/s   SUM   4.79 GB/s

Peak=10.280 GB/s  Util~=46.6%

PORT  SOURCE    READ           WRITE          TOTAL          AGE
   0  RPU         ...
   1  CCI-0       ...
   2  CCI-1       ...
   3  HP0/DP      ...
   4  HP1/2       ...
   5  HP3/DMA     ...
```

Read traffic is shown in **green**, while write traffic is shown in **cyan**.

## Counter Handling

`ddrtop` does not reset the APM metric counters after each sample.

Instead, it:

1. stops metric counting;
2. programs the metric selectors;
3. reads the initial counter values;
4. starts counting;
5. waits for the configured sampling interval;
6. stops counting;
7. reads the final values;
8. computes modular counter deltas.

This approach avoids continuously resetting the hardware counters and correctly handles 32-bit counter wrap-around.

The GCCR counter is read using an **H-L-H** sequence to avoid torn 64-bit reads when the lower half rolls over between register accesses.

## Counter Overflow

Metric byte counters are 32-bit.

When `--ddr-peak-gbps` is provided, `ddrtop` checks whether the requested sampling interval could approach a 32-bit byte-counter wrap at the specified bandwidth.

If the interval is unsafe, the program exits and requests a shorter sampling interval.

For high-bandwidth systems, a relatively short interval such as:

```bash
--interval 0.1
```

is recommended.

## Permissions

`ddrtop` accesses physical memory directly using:

```text
/dev/mem
```

and therefore normally requires root privileges:

```bash
sudo ./ddrtop.py
```

If the program is executed without sufficient permissions, it will terminate with an error.

## Notes

`ddrtop` is intended for low-level performance analysis of DDR traffic on Zynq UltraScale+ MPSoC platforms.

Because APMDDR provides only ten metric counters, the six DDR ports cannot all be measured with simultaneous RD/WR counters during the same sampling interval. The `ALL~ cached` value should therefore be interpreted as an estimate when traffic changes significantly between adjacent sampling windows.

For temporally coherent measurements, use the `WINDOW exact` value.
