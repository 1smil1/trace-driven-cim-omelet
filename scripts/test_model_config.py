"""Smoke-test a model-sized topology through the persistent gem5 bridge."""

import argparse
from time import perf_counter
from pathlib import Path

from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import (
    EXAMPLE_BONDING,
    EXAMPLE_MATERIAL,
    write_model_configs,
)
from omelet.backends.gem5.online import OnlineGem5Session


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k-chip", type=int, required=True)
    parser.add_argument("--k-node", type=int, required=True)
    parser.add_argument("--destination", type=int)
    parser.add_argument("--ticks", type=int, default=1_000_000)
    parser.add_argument("--control-buffers", type=int, default=8)
    parser.add_argument("--size", type=int, default=64)
    parser.add_argument("--batches", type=int, nargs="+", default=[1])
    parser.add_argument("--overlap-ticks", type=int)
    args = parser.parse_args()
    if any(count <= 0 for count in args.batches):
        parser.error("batch packet counts must be positive")
    if args.overlap_ticks is not None and not 0 < args.overlap_ticks < args.ticks:
        parser.error("overlap ticks must be positive and less than the deadline")
    outdir = Path(__file__).resolve().parents[1] / "output" / f"model_grid_{args.k_chip}_{args.k_node}"
    configs = write_model_configs(outdir / "config", k_chip=args.k_chip, k_node=args.k_node)
    routers = args.k_chip * args.k_chip * args.k_node * args.k_node
    if args.k_chip < 2:
        parser.error("cross-chiplet smoke test needs at least two chiplets per side")
    cmd = build_gem5_cmd(
        topology="mesh", material=EXAMPLE_MATERIAL, bonding=EXAMPLE_BONDING,
        injection_rate=0.0, sim_cycles=100000,
        synthetic="uniform_random", outdir=str(outdir / "gem5"), num_cpus=routers,
        sys_config=str(configs["system"]), net_config=str(configs["network"]),
        noc_config=str(configs["noc"]), noi_config=str(configs["noi"]),
        plc_config=str(configs["placement"]),
    )
    cmd.append(f"--buffers-per-ctrl-vc={args.control_buffers}")
    cmd.append(f"--buffers-per-data-vc={args.control_buffers}")
    cmd.append("--xor-low-bit=0")
    startup_start = perf_counter()
    with OnlineGem5Session(cmd, outdir / "gem5") as session:
        handshake_seconds = perf_counter() - startup_start
        warmup_start = perf_counter()
        warmup = session.advance(1)
        if warmup["arrivals"] or session.in_flight() != 0:
            raise RuntimeError("empty-network warmup produced traffic")
        warmup_seconds = perf_counter() - warmup_start
        destination = args.k_node * args.k_node if args.destination is None else args.destination
        if not args.k_node * args.k_node <= destination < routers:
            parser.error(f"destination must be in [{args.k_node * args.k_node}, {routers - 1}]")
        print(f"model grid: {args.k_chip}x{args.k_chip} chiplets, {routers} routers, handshake_s={handshake_seconds:.3f} warmup_s={warmup_seconds:.3f}", flush=True)
        if args.overlap_ticks is not None:
            traffic_start = perf_counter()
            arrivals = []
            next_id = 1
            current_tick = warmup["tick"]
            for task_number, count in enumerate(args.batches, 1):
                for _ in range(count):
                    session.inject(0, destination, args.size, next_id)
                    next_id += 1
                if task_number < len(args.batches):
                    progress = session.advance(args.overlap_ticks)
                    current_tick = progress["tick"]
                    arrivals.extend(progress["arrivals"])
                    if session.in_flight() == 0:
                        raise RuntimeError(f"task {task_number} did not overlap the next task")
            while len(arrivals) < next_id - 1 and current_tick - warmup["tick"] < args.ticks:
                progress = session.advance(args.ticks - (current_tick - warmup["tick"]))
                current_tick = progress["tick"]
                arrivals.extend(progress["arrivals"])
            if len(arrivals) != next_id - 1 or {a["id"] for a in arrivals} != set(range(1, next_id)):
                raise RuntimeError(f"overlapping tasks incomplete: {arrivals}")
            if session.in_flight() != 0:
                raise RuntimeError("overlapping tasks left packets in flight")
            print(f"overlap tasks={len(args.batches)} packets={next_id - 1} traffic_s={perf_counter() - traffic_start:.3f} last_arrival_tick={max(a['tick'] for a in arrivals) - warmup['tick']}", flush=True)
            return
        next_id = 1
        for count in args.batches:
            traffic_start = perf_counter()
            first_id = next_id
            base_tick = session.inject(0, destination, args.size, next_id)
            next_id += 1
            for _ in range(count - 1):
                if session.inject(0, destination, args.size, next_id) != base_tick:
                    raise RuntimeError("batch injections did not share a simulation tick")
                next_id += 1
            inject_seconds = perf_counter() - traffic_start
            arrivals = []
            tick = base_tick
            while len(arrivals) < count and tick - base_tick < args.ticks:
                result = session.advance(args.ticks - (tick - base_tick))
                tick = result["tick"]
                arrivals.extend(result["arrivals"])
            if len(arrivals) != count or {a["id"] for a in arrivals} != set(range(first_id, next_id)):
                raise RuntimeError(f"batch of {count} packets incomplete at tick {tick}: {arrivals}")
            if session.in_flight() != 0:
                raise RuntimeError(f"batch of {count} packets left in flight at tick {tick}")
            traffic_seconds = perf_counter() - traffic_start
            print(f"packets={count} bytes_each={args.size} inject_s={inject_seconds:.3f} traffic_s={traffic_seconds:.3f} sim_ticks={tick - base_tick} first_arrival_tick={min(a['tick'] for a in arrivals) - base_tick} last_arrival_tick={max(a['tick'] for a in arrivals) - base_tick}", flush=True)


if __name__ == "__main__":
    main()
