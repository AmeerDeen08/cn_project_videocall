#!/usr/bin/env python3
"""Loss-versus-quality study: no FEC vs fixed FEC vs adaptive FEC.

For every loss level, two headless peers stream the sample clip to each other
for a fixed time in each FEC mode while packet loss is injected.  The
receiving peer's per-frame CSV is then summarised into

    results/comparison.csv        one row per (loss level, mode)
    results/summary.json          the same, loaded by the dashboard Results view
    results/psnr_vs_loss.png      picture quality vs loss
    results/recovery_vs_loss.png  frames recovered vs loss
    results/overhead_vs_loss.png  FEC overhead vs loss
    results/ssim_vs_loss.png      structural similarity vs loss
    results/adaptation_trace.png  adaptive FEC following a loss step pattern

Loss is injected with tc netem on the loopback interface (needs sudo; you are
asked for your password once), or with --impair sim, by dropping packets
inside the peers -- no root needed, same Bernoulli loss model.
"""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
MODES = ["off", "fixed", "adaptive"]
LABELS = {"off": "No FEC", "fixed": "Fixed FEC", "adaptive": "Adaptive FEC"}
COLORS = {"off": "#1baf7a", "fixed": "#eb6834", "adaptive": "#2a78d6"}
PORT_A, PORT_B = 5000, 6000


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--levels", default="0,5,10,15,20,25,30",
                   help="comma-separated loss percentages")
    p.add_argument("--duration", type=float, default=12, help="measured seconds per run")
    p.add_argument("--warmup", type=float, default=3, help="seconds discarded per run")
    p.add_argument("--fixed-redundancy", type=int, default=5)
    p.add_argument("--impair", choices=["auto", "netem", "sim"], default="auto")
    p.add_argument("--iface", default="lo")
    p.add_argument("--source", default=os.path.join(HERE, "samples", "sample.mp4"))
    p.add_argument("--out", default=os.path.join(HERE, "results"))
    p.add_argument("--no-trace", action="store_true",
                   help="skip the adaptive step-response run")
    p.add_argument("--replot", action="store_true",
                   help="only rebuild graphs/summary from existing run CSVs")
    return p.parse_args()


# ------------------------------------------------------------- impairment

class Impairment:
    def __init__(self, kind, iface):
        self.kind = kind
        self.iface = iface
        self.script = os.path.join(HERE, "impair.sh")

    def set(self, loss):
        if self.kind == "netem":
            subprocess.run(["sudo", "-n", self.script, "add", self.iface,
                            "--loss", str(loss)], check=True,
                           stdout=subprocess.DEVNULL)

    def clear(self):
        if self.kind == "netem":
            subprocess.run(["sudo", "-n", self.script, "clear", self.iface],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def peer_args(self, loss):
        return ["--sim-loss", str(loss)] if self.kind == "sim" else []


def choose_impairment(requested, iface):
    if requested == "sim":
        return Impairment("sim", iface)
    usable = sys.platform.startswith("linux") and \
        subprocess.run(["which", "tc"], capture_output=True).returncode == 0
    if usable and subprocess.run(["sudo", "-n", "true"],
                                 capture_output=True).returncode != 0:
        print("tc netem needs root; sudo may ask for your password once.",
              file=sys.stderr)
        usable = subprocess.run(["sudo", "-S", "-v"]).returncode == 0
    if usable:
        return Impairment("netem", iface)
    if requested == "netem":
        sys.exit("tc netem is not available (needs Linux + sudo); "
                 "use --impair sim")
    print("falling back to in-app loss simulation (--impair sim)", file=sys.stderr)
    return Impairment("sim", iface)


# ------------------------------------------------------------------- runs

def run_pair(mode, loss, seconds, csv_path, args, imp):
    common = ["--mode", "file", "--source", args.source, "--fec", mode,
              "--fixed-redundancy", str(args.fixed_redundancy),
              "--no-window", "--quiet", "--duration", str(seconds)]
    common += imp.peer_args(loss)
    b = subprocess.Popen([PY, os.path.join(HERE, "peer.py"),
                          "--listen-port", str(PORT_B), "--peer-port", str(PORT_A),
                          "--metrics-csv", csv_path] + common)
    a = subprocess.Popen([PY, os.path.join(HERE, "peer.py"),
                          "--listen-port", str(PORT_A), "--peer-port", str(PORT_B),
                          "--metrics-csv", os.devnull, "--no-quality"] + common)
    return a, b


def wait_all(procs, timeout):
    for p in procs:
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()


def read_rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def fnum(v):
    return float(v) if v not in ("", None) else math.nan


def mean(values):
    values = [v for v in values if not math.isnan(v)]
    return sum(values) / len(values) if values else math.nan


def summarise(path, warmup):
    rows = read_rows(path)
    if not rows:
        return None
    t0 = float(rows[0]["t"])
    rows = [r for r in rows if float(r["t"]) >= t0 + warmup]
    if not rows:
        return None
    sent = sum(int(r["n"]) for r in rows)
    got = sum(int(r["received"]) for r in rows)
    outcomes = {o: sum(r["outcome"] == o for r in rows)
                for o in ("intact", "rescued", "damaged", "lost")}
    return {
        "frames": len(rows),
        "measured_loss_pct": 100 * (1 - got / sent) if sent else math.nan,
        "recovery_pct": 100 * sum(r["recovered"] == "1" for r in rows) / len(rows),
        "rescued_pct": 100 * outcomes["rescued"] / len(rows),
        "psnr": mean([fnum(r["psnr"]) for r in rows]),
        "ssim": mean([fnum(r["ssim"]) for r in rows]),
        "overhead_pct": 100 * sum(int(r["r"]) for r in rows)
        / max(1, sum(int(r["k"]) for r in rows)),
        "latency_ms": mean([fnum(r["latency_ms"]) for r in rows]),
        **{f"{o}_frames": n for o, n in outcomes.items()},
    }


def sweep(args, imp, levels, runs_dir):
    total = len(levels) * len(MODES)
    i = 0
    for loss in levels:
        for mode in MODES:
            i += 1
            path = os.path.join(runs_dir, f"{mode}_{loss:g}.csv")
            print(f"[{i}/{total}] loss {loss:g}%  {LABELS[mode]:<13}", end="  ",
                  flush=True)
            imp.set(loss)
            seconds = args.warmup + args.duration
            procs = run_pair(mode, loss, seconds, path, args, imp)
            wait_all(procs, seconds + 20)
            s = summarise(path, args.warmup)
            if s:
                print(f"measured loss {s['measured_loss_pct']:5.1f}%  "
                      f"recovered {s['recovery_pct']:5.1f}%  "
                      f"PSNR {s['psnr']:5.1f} dB  overhead {s['overhead_pct']:4.0f}%")
            else:
                print("no frames received!")
            time.sleep(0.5)
    imp.clear()


def trace(args, imp, runs_dir):
    """Adaptive mode under a loss step pattern (netem only)."""
    steps = [(0, 10), (10, 10), (20, 10), (30, 10), (5, 10)]
    path = os.path.join(runs_dir, "trace_adaptive.csv")
    print("adaptation trace: loss steps " +
          " -> ".join(f"{loss}%" for loss, _ in steps))
    imp.set(steps[0][0])
    total = sum(d for _, d in steps) + 2
    procs = run_pair("adaptive", 0, total, path, args, imp)
    start = time.time()
    schedule = []
    elapsed = 0
    for loss, dur in steps:
        imp.set(loss)
        schedule.append((time.time() - start, loss))
        elapsed += dur
        time.sleep(max(0, start + elapsed - time.time()))
    wait_all(procs, 30)
    imp.clear()
    with open(os.path.join(runs_dir, "trace_schedule.json"), "w") as f:
        json.dump({"start": start, "steps": schedule}, f)


# --------------------------------------------------------------- outputs

def collect(args, levels, runs_dir):
    rows = []
    for loss in levels:
        for mode in MODES:
            path = os.path.join(runs_dir, f"{mode}_{loss:g}.csv")
            if not os.path.exists(path):
                continue
            s = summarise(path, args.warmup)
            if s:
                rows.append({"loss": loss, "mode": mode, **s})
    return rows


def write_outputs(args, rows, levels, kind, out):
    fields = ["loss", "mode", "frames", "measured_loss_pct", "recovery_pct",
              "rescued_pct", "psnr", "ssim", "overhead_pct", "latency_ms",
              "intact_frames", "rescued_frames", "damaged_frames", "lost_frames"]
    with open(os.path.join(out, "comparison.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: (round(v, 4) if isinstance(v, float) else v)
                        for k, v in r.items() if k in fields})

    def clean(v, nd):
        return None if isinstance(v, float) and math.isnan(v) else round(v, nd)

    graphs = [g for g in ("psnr_vs_loss.png", "recovery_vs_loss.png",
                          "overhead_vs_loss.png", "ssim_vs_loss.png",
                          "adaptation_trace.png")
              if os.path.exists(os.path.join(out, g))]
    summary = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "loss_levels": levels,
        "modes": MODES,
        "duration_s": args.duration,
        "warmup_s": args.warmup,
        "fixed_redundancy": args.fixed_redundancy,
        "impairment": kind,
        "rows": [{"loss": r["loss"], "mode": r["mode"], "frames": r["frames"],
                  "measured_loss_pct": clean(r["measured_loss_pct"], 2),
                  "recovery_pct": clean(r["recovery_pct"], 2),
                  "rescued_pct": clean(r["rescued_pct"], 2),
                  "psnr": clean(r["psnr"], 2), "ssim": clean(r["ssim"], 4),
                  "overhead_pct": clean(r["overhead_pct"], 2),
                  "latency_ms": clean(r["latency_ms"], 2)} for r in rows],
        "files": {"csv": "comparison.csv", "graphs": graphs},
    }
    with open(os.path.join(out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)


def plot(rows, levels, out, runs_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.size": 10, "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#c7c7cc", "axes.labelcolor": "#3a3a3c",
        "xtick.color": "#6e6e73", "ytick.color": "#6e6e73",
        "axes.grid": True, "grid.color": "#e5e5ea", "grid.linewidth": 0.8,
        "axes.axisbelow": True, "legend.frameon": False,
    })

    def chart(key, ylabel, title, fname, ylim=None):
        fig, ax = plt.subplots(figsize=(6.4, 4.0), dpi=150)
        for mode in MODES:
            pts = [(r["loss"], r[key]) for r in rows if r["mode"] == mode]
            if pts:
                xs, ys = zip(*pts)
                ax.plot(xs, ys, marker="o", markersize=6, linewidth=2,
                        color=COLORS[mode], label=LABELS[mode],
                        markeredgecolor="white", markeredgewidth=1.2)
        ax.set_xlabel("Injected packet loss (%)")
        ax.set_ylabel(ylabel)
        ax.set_title(title, loc="left", fontsize=12, color="#1d1d1f")
        ax.set_xticks(levels)
        if ylim:
            ax.set_ylim(*ylim)
        ax.grid(axis="x", visible=False)
        ax.legend(loc="best")
        fig.tight_layout()
        fig.savefig(os.path.join(out, fname))
        plt.close(fig)

    chart("psnr", "Mean PSNR of displayed frames (dB)",
          "Picture quality vs packet loss", "psnr_vs_loss.png", (0, None))
    chart("recovery_pct", "Frames fully recovered (%)",
          "Frame recovery vs packet loss", "recovery_vs_loss.png", (0, 105))
    chart("overhead_pct", "FEC overhead, parity / data (%)",
          "Bandwidth overhead vs packet loss", "overhead_vs_loss.png", (0, None))
    chart("ssim", "Mean SSIM of displayed frames",
          "Structural similarity vs packet loss", "ssim_vs_loss.png", (0, 1.02))

    trace_csv = os.path.join(runs_dir, "trace_adaptive.csv")
    sched_path = os.path.join(runs_dir, "trace_schedule.json")
    if os.path.exists(trace_csv) and os.path.exists(sched_path):
        with open(sched_path) as f:
            sched = json.load(f)
        trows = read_rows(trace_csv)
        if trows:
            t0 = sched["start"]
            buckets = {}
            for r in trows:
                b = int((float(r["t"]) - t0) * 2) / 2          # 0.5 s bins
                buckets.setdefault(b, []).append(r)
            ts = sorted(buckets)
            loss = [100 * (1 - sum(int(r["received"]) for r in buckets[t])
                           / sum(int(r["n"]) for r in buckets[t])) for t in ts]
            over = [100 * sum(int(r["r"]) for r in buckets[t])
                    / sum(int(r["k"]) for r in buckets[t]) for t in ts]
            rec = [100 * sum(r["recovered"] == "1" for r in buckets[t])
                   / len(buckets[t]) for t in ts]
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(7.2, 5.2), dpi=150,
                                           sharex=True,
                                           gridspec_kw={"height_ratios": [2, 1]})
            steps_t = [s[0] for s in sched["steps"]] + [ts[-1]]
            steps_l = [s[1] for s in sched["steps"]]
            ax1.step(steps_t, steps_l + [steps_l[-1]], where="post", color="#86868b",
                     linewidth=1.2, linestyle="--", label="Injected loss (netem)")
            ax1.plot(ts, loss, color=COLORS["fixed"], linewidth=2,
                     label="Measured loss")
            ax1.plot(ts, over, color=COLORS["adaptive"], linewidth=2,
                     label="Redundancy r / k")
            ax1.set_ylabel("Percent")
            ax1.set_title("Adaptive FEC following a changing loss rate",
                          loc="left", fontsize=12, color="#1d1d1f")
            ax1.legend(loc="upper left")
            ax1.grid(axis="x", visible=False)
            ax2.plot(ts, rec, color="#0ca30c", linewidth=2)
            ax2.set_ylim(0, 105)
            ax2.set_ylabel("Frames\nrecovered (%)")
            ax2.set_xlabel("Time (s)")
            ax2.grid(axis="x", visible=False)
            fig.tight_layout()
            fig.savefig(os.path.join(out, "adaptation_trace.png"))
            plt.close(fig)


def print_table(rows, levels):
    print()
    print(f"{'loss':>5} | {'recovered %':^23} | {'PSNR dB':^23} | {'overhead %':^20}")
    print(f"{'':>5} | " + " | ".join(
        " ".join(f"{m[:7]:>7}" for m in MODES) for _ in range(3)))
    for loss in levels:
        by = {r["mode"]: r for r in rows if r["loss"] == loss}

        def cells(key, nd):
            return " ".join(f"{by[m][key]:7.{nd}f}" if m in by else f"{'-':>7}"
                            for m in MODES)
        print(f"{loss:>4g}% | {cells('recovery_pct', 1)} | {cells('psnr', 1)} | "
              f"{cells('overhead_pct', 0)}")


def main():
    args = parse_args()
    levels = [float(x) if "." in x else int(x) for x in args.levels.split(",")]
    out = args.out
    runs_dir = os.path.join(out, "runs")
    os.makedirs(runs_dir, exist_ok=True)
    if not os.path.exists(args.source):
        sys.exit(f"missing {args.source}; run: python make_sample.py")

    kind_path = os.path.join(runs_dir, "impairment.txt")
    if args.replot:
        kind = open(kind_path).read().strip() if os.path.exists(kind_path) else "netem"
    else:
        imp = choose_impairment(args.impair, args.iface)
        kind = imp.kind
        with open(kind_path, "w") as f:
            f.write(kind)
        print(f"impairment: {kind}; {len(levels)} levels x {len(MODES)} modes, "
              f"{args.warmup:g}+{args.duration:g} s each")
        try:
            sweep(args, imp, levels, runs_dir)
            if not args.no_trace and kind == "netem":
                trace(args, imp, runs_dir)
        finally:
            imp.clear()

    rows = collect(args, levels, runs_dir)
    plot(rows, levels, out, runs_dir)
    write_outputs(args, rows, levels, kind, out)
    print_table(rows, levels)
    print(f"\nwrote {out}/comparison.csv, summary.json and graphs; open the "
          "dashboard's Results tab to view them.")


if __name__ == "__main__":
    main()
