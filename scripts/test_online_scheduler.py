"""Run two-input compute/communication and optional reuse through Garnet."""

import argparse
import json
from pathlib import Path

from chip.comm.omelet_mapping import OmeletNodeMap
from chip.comm.omelet_scheduler import run_omelet_workload
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import EXAMPLE_BONDING, EXAMPLE_MATERIAL, write_model_configs
from omelet.backends.gem5.online import OnlineGem5Session


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dynamic-reuse", action="store_true")
    args = parser.parse_args()
    out = Path(__file__).resolve().parents[1] / "output" / "online_scheduler_smoke"
    out.mkdir(parents=True, exist_ok=True)
    configs = write_model_configs(out / "config", k_chip=2, k_node=2)
    command = build_gem5_cmd(
        topology="mesh", material=EXAMPLE_MATERIAL, bonding=EXAMPLE_BONDING,
        injection_rate=0.0, sim_cycles=100000, synthetic="uniform_random",
        outdir=str(out / "gem5"), num_cpus=16,
        sys_config=str(configs["system"]), net_config=str(configs["network"]),
        noc_config=str(configs["noc"]), noi_config=str(configs["noi"]),
        plc_config=str(configs["placement"]),
    )
    command.extend(("--buffers-per-ctrl-vc=8", "--xor-low-bit=0"))
    rows = []
    for node_id in range(16):
        chiplet, local = divmod(node_id, 4)
        x = (chiplet % 2) * 2 + local % 2
        y = (chiplet // 2) * 2 + local // 2
        rows.append(f"{node_id} {x} {y} {chiplet} {chiplet} real {y * 4 + x}")
    position = out / "trace_node_position_mapping.txt"
    position.write_text("\n".join(rows) + "\n", encoding="utf-8")
    templates = out / "templates"
    templates.mkdir(exist_ok=True)
    for group_id, (destination, size) in enumerate(((4, 64), (8, 1024))):
        (templates / f"group_{group_id}.json").write_text(json.dumps({
            "metadata": {"shared_prefix_policy": 0},
            "packets": [{"src": 10 if args.dynamic_reuse else 0,
                         "dst": 11 if args.dynamic_reuse else destination,
                         "custom_size_bytes": size}],
        }), encoding="utf-8")
    manifest = {
        "num_inputs": 2, "num_resources": 16 if args.dynamic_reuse else 3,
        "template_groups": [
            {"group_id": group_id, "bz2_path": f"templates/group_{group_id}.bz2"}
            for group_id in (0, 1)
        ],
        "phases": [
            {"phase_id": 0, "input_id": 0, "layer_id": 0,
             "compute_latency_cycles": 10, "dep_phase_ids": [],
             "resource_ids": [0], "group_id": 0},
            {"phase_id": 1, "input_id": 0, "layer_id": 1,
             "compute_latency_cycles": 5, "dep_phase_ids": [0],
             "resource_ids": [1], "group_id": -1},
            {"phase_id": 2, "input_id": 1, "layer_id": 0,
             "compute_latency_cycles": 10, "dep_phase_ids": [],
             "resource_ids": [0 if args.dynamic_reuse else 2], "group_id": 1},
        ],
    }
    if args.dynamic_reuse:
        manifest["endpoint_mapping_catalog"] = "endpoint_mapping_catalog.json"
        (out / "endpoint_mapping_catalog.json").write_text(json.dumps({
            "auto_reuse": 1, "resolve_mode": 1,
            "physical_nodes": [
                {"physical_id": node_id, "tier_id": node_id // 4}
                for node_id in range(16)
            ],
            "layers": [
                {"layer_id": 0, "block_id": 0, "virtual_ids": [10],
                 "canonical": [0], "candidates": [[0], [1]]},
                {"layer_id": 1, "block_id": -1, "virtual_ids": [11],
                 "canonical": [4], "candidates": [[4]]},
            ],
        }), encoding="utf-8")
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with OnlineGem5Session(command, out / "gem5") as session:
        result = run_omelet_workload(
            manifest_path, OmeletNodeMap(position, k_node=2, k_chip=2), session, 1.0
        )
    assert result["message_arrived"] == 2, result
    assert result["phases"][1]["compute_start_cycle"] < result["inputs"][1]["input_done_cycle"], result
    if args.dynamic_reuse:
        assert [phase["resource_ids"] for phase in result["phases"]] == [[0], [4], [1]], result
    (out / "sim_output.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Omelet scheduler OK: {result['inputs']}")


if __name__ == "__main__":
    main()
