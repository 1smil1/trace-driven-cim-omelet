"""Run a generated chip online manifest through one real Omelet session."""

import argparse
import json
from pathlib import Path
from time import perf_counter

from chip.comm.omelet_mapping import OmeletNodeMap
from chip.comm.omelet_scheduler import run_omelet_workload, validate_omelet_endpoints
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import EXAMPLE_BONDING, EXAMPLE_MATERIAL, write_model_configs
from omelet.backends.gem5.online import OnlineGem5Session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--k-chip", type=int, required=True)
    parser.add_argument("--k-node", type=int, required=True)
    parser.add_argument("--frequency-ghz", type=float, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    manifest = args.manifest.resolve()
    out = Path(__file__).resolve().parents[1] / "output" / "model_manifest_smoke"
    out.mkdir(parents=True, exist_ok=True)
    configs = write_model_configs(out / "config", k_chip=args.k_chip, k_node=args.k_node)
    routers = args.k_chip * args.k_chip * args.k_node * args.k_node
    command = build_gem5_cmd(
        topology="mesh", material=EXAMPLE_MATERIAL, bonding=EXAMPLE_BONDING,
        injection_rate=0.0, sim_cycles=100000, synthetic="uniform_random",
        outdir=str(out / "gem5"), num_cpus=routers,
        sys_config=str(configs["system"]), net_config=str(configs["network"]),
        noc_config=str(configs["noc"]), noi_config=str(configs["noi"]),
        plc_config=str(configs["placement"]),
    )
    command.extend(("--buffers-per-ctrl-vc=8", "--xor-low-bit=0"))
    positions = manifest.parent / "trace_node_position_mapping.txt"
    node_map = OmeletNodeMap(positions, k_node=args.k_node, k_chip=args.k_chip)
    if node_map.router_count != routers:
        raise ValueError(f"position map has {node_map.router_count} routers, expected {routers}")
    packet_count = validate_omelet_endpoints(manifest, node_map)
    print(f"Validated {packet_count} model template packets", flush=True)
    if args.validate_only:
        return
    started = perf_counter()
    with OnlineGem5Session(command, out / "gem5") as session:
        print(f"gem5 ready after {perf_counter() - started:.2f}s", flush=True)
        result = run_omelet_workload(manifest, node_map, session, args.frequency_ghz)
        print(f"traffic completed after {perf_counter() - started:.2f}s", flush=True)
    result_path = out / "sim_output.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Omelet result: {result_path}; inputs={result['inputs']}", flush=True)


if __name__ == "__main__":
    main()
