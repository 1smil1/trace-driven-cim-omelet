"""Built-gem5 check: two sized packets arrive through one persistent process."""

import re
import os
import argparse
from pathlib import Path

from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.online import OnlineGem5Session


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/online_bridge_smoke"
parser = argparse.ArgumentParser()
parser.add_argument("--debug", action="store_true")
parser.add_argument("--reference-stats", type=Path,
                    help="With --debug, compare all non-host statistics with a prior dump")
parser.add_argument("--verify-native-power-state", action="store_true")
parser.add_argument("--native-export-stats", action="store_true",
                    help="Test-only: export full stats from the normal native configuration")
parser.add_argument("--route-weight", type=int, choices=(0, 1, 2, 3), default=0,
                    help="With native-state checks, exercise bounded/heap shortest paths")
parser.add_argument("--distinct-vnets", action="store_true",
                    help="With native-state checks, keep one vnet graph distinct")
parser.add_argument("--distinct-endpoints", action="store_true",
                    help="With native-state checks, reject terminal-type route reuse")
parser.add_argument("--reference-routes", type=Path,
                    help="Compare exact small-network route digests with a prior stdout")
args = parser.parse_args()
if args.reference_stats is not None and not (args.debug or args.native_export_stats):
    parser.error("--reference-stats requires --debug or --native-export-stats")
if args.native_export_stats and not args.verify_native_power_state:
    parser.error("--native-export-stats requires --verify-native-power-state")
if args.verify_native_power_state and args.debug:
    parser.error("--verify-native-power-state checks the non-debug native path")
if args.route_weight != 0 and not args.verify_native_power_state:
    parser.error("--route-weight requires --verify-native-power-state")
if args.distinct_vnets and not args.verify_native_power_state:
    parser.error("--distinct-vnets requires --verify-native-power-state")
if args.distinct_endpoints and not args.verify_native_power_state:
    parser.error("--distinct-endpoints requires --verify-native-power-state")
if args.reference_routes is not None:
    os.environ["OMELET_ROUTE_DIGEST"] = "1"
# Repeated normal/debug checks share this test-only output directory.
for filename in ("stats.txt", "link_epb.csv"):
    (OUT / filename).unlink(missing_ok=True)
cmd = build_gem5_cmd(
    topology="mesh", material="org", injection_rate=0.0,
    sim_cycles=10000, synthetic="uniform_random", outdir=str(OUT),
    net_config=str(ROOT / "examples/network/network_2_5d.yaml"),
)
if args.verify_native_power_state:
    os.environ["OMELET_TEST_ROUTE_WEIGHT"] = str(args.route_weight)
    if args.native_export_stats:
        os.environ["OMELET_TEST_NATIVE_EXPORT"] = "1"
    else:
        os.environ.pop("OMELET_TEST_NATIVE_EXPORT", None)
    if args.distinct_vnets:
        os.environ["OMELET_TEST_DISTINCT_VNETS"] = "1"
    else:
        os.environ.pop("OMELET_TEST_DISTINCT_VNETS", None)
    if args.distinct_endpoints:
        os.environ["OMELET_TEST_DISTINCT_ENDPOINTS"] = "1"
    else:
        os.environ.pop("OMELET_TEST_DISTINCT_ENDPOINTS", None)
    config_args = [i for i, arg in enumerate(cmd)
                   if Path(arg).name == "garnet_synth_traffic.py"]
    assert len(config_args) == 1, cmd
    cmd[config_args[0]] = str(ROOT / "scripts/check_native_power_state_config.py")
with OnlineGem5Session(cmd, OUT, debug=args.debug) as gem5:
    gem5.inject(0, 1, 64, 101)
    gem5.inject(0, 2, 1024, 102)
    first = gem5.advance(1_000_000)
    assert first["arrivals"] == [{"id": 101, "tick": first["tick"]}], first
    assert gem5.in_flight() == 1
    assert gem5.inject(1, 0, 64, 103) == first["tick"]
    arrivals = first["arrivals"]
    while len(arrivals) < 3:
        arrivals.extend(gem5.advance(1_000_000)["arrivals"])
    assert {row["id"] for row in arrivals} == {101, 102, 103}, arrivals
    assert arrivals[0]["tick"] < arrivals[1]["tick"], arrivals
    assert gem5.in_flight() == 0

assert arrivals == [
    {"id": 101, "tick": 6993},
    {"id": 103, "tick": 12987},
    {"id": 102, "tick": 48951},
], arrivals
if args.verify_native_power_state:
    engine = "bounded-weight FIFO" if args.route_weight <= 2 else "Dijkstra"
    output = (OUT / "gem5_stdout.log").read_text(encoding="utf-8")
    assert f"engine={engine}" in output
    source_vnet = 2 if args.distinct_vnets else 1
    assert f"vnet=3/3 identical graph; shared routes from vnet={source_vnet}" in output
    reuse = "equivalent terminal type=" in output
    assert reuse != args.distinct_endpoints, output
if args.reference_routes is not None:
    def route_digests(path):
        values = dict(re.findall(r"OMELET_ROUTE_DIGEST (\w+)=(0x[0-9a-f]+)",
                                 path.read_text(encoding="utf-8")))
        assert set(values) == {"weights", "dist", "marking"}, values
        return values

    assert route_digests(OUT / "gem5_stdout.log") == route_digests(args.reference_routes)
    print("reference route digests matched: weights, distances, all destination masks")
if args.debug or args.native_export_stats:
    assert (OUT / "link_epb.csv").is_file() == args.debug
    stats = (OUT / "stats.txt").read_text(encoding="utf-8")
    flits = re.search(r"^system\.ruby\.network\.flits_received::total\s+(\d+)", stats, re.M)
    packets = re.search(r"^system\.ruby\.network\.packets_received::total\s+(\d+)", stats, re.M)
    assert flits and int(flits.group(1)) == 72, flits
    assert packets and int(packets.group(1)) == 3, packets
    # Sparse per-source histograms must retain every original pair count.
    pair_counts = {
        (int(src), int(dst)): int(count)
        for src, dst, count in re.findall(
            r"^system\.ruby\.network\.data_traffic_distribution\.n(\d+)::(\d+)\s+(\d+)",
            stats, re.M,
        )
    }
    assert pair_counts == {(0, 1): 1, (0, 2): 1, (1, 0): 1}, pair_counts
    if args.reference_stats is not None:
        def records(text):
            result = []
            for line in text.splitlines():
                body = line.partition("#")[0]
                # gem5 prints units outside '#' when the description is empty.
                body = re.sub(r"\s+\(.*\)\s*$", "", body)
                fields = tuple(body.split())
                if not fields or fields[0].startswith("-"):
                    continue
                # Host performance/memory changes with initialization and load.
                if fields[0] in {"hostSeconds", "hostTickRate", "hostMemory",
                                 "hostInstRate", "hostOpRate"}:
                    continue
                if args.debug:
                    # Debug must retain the original display descriptions too.
                    fields += (line.partition("#")[2].strip(),)
                result.append(fields)
            return result

        before = records(args.reference_stats.read_text(encoding="utf-8"))
        after = records(stats)
        assert len(before) == len(after), (len(before), len(after))
        for expected, actual in zip(before, after):
            assert expected == actual, (expected, actual)
        print(f"reference statistics matched: {len(after)} records")
else:
    assert not (OUT / "link_epb.csv").exists()
    stats_path = OUT / "stats.txt"
    assert not stats_path.exists() or stats_path.stat().st_size == 0
print("online bridge OK: exact arrival ticks matched, packets=3")
