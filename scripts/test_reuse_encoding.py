"""Actual Garnet regression: prefer encoded resources, keep idle alternatives."""

import argparse
import json
from pathlib import Path

from chip.comm.omelet_mapping import OmeletNodeMap
from chip.comm.omelet_scheduler import run_omelet_workload
from chip.comm.online_workload_builder import _write_bz2_group
from chip.comm.runtime_energy import annotate_endpoint_schedule
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import (
    EXAMPLE_BONDING, EXAMPLE_MATERIAL, write_model_configs,
)
from omelet.backends.gem5.online import OnlineGem5Session


def check(out, engine, enabled):
    out.mkdir(parents=True, exist_ok=True)
    configs = write_model_configs(out / "config", k_chip=2, k_node=2)
    position = out / "positions.txt"
    rows = []
    for node in range(16):
        chip, local = divmod(node, 4)
        x, y = (chip % 2) * 2 + local % 2, (chip // 2) * 2 + local // 2
        rows.append(f"{node} {x} {y} 0 {chip} {chip} real {y * 4 + x}")
    position.write_text("\n".join(rows) + "\n", encoding="utf-8")
    _write_bz2_group(str(out / "group.bz2"),
                     [{"src": 10, "dst": 20, "size_bytes": 64, "shared_key": -1}],
                     debug=False)
    catalog = {
        "auto_reuse": 1, "resolve_mode": 1,
        "virtual_tiles": [{"virtual_id": 10, "physical_id": 1},
                          {"virtual_id": 20, "physical_id": 3}],
        "physical_nodes": [{"physical_id": i, "tier_id": i // 4} for i in range(16)],
        "layers": [
            {"layer_id": 0, "block_id": 0, "virtual_ids": [10],
             "canonical": [1], "candidates": [[0], [1]]},
            {"layer_id": 1, "block_id": -1, "virtual_ids": [20],
             "canonical": [3], "candidates": [[3]]},
        ],
    }
    catalog_path = out / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    manifest = {
        "num_inputs": 3, "num_resources": 16,
        "endpoint_mapping_catalog": "catalog.json",
        "template_groups": [{"group_id": 0, "bz2_path": "group.bz2", "shared_prefix_policy": 0}],
        "phases": [
            {"phase_id": i, "input_id": i, "layer_id": 0,
             "compute_latency_cycles": 10, "dep_phase_ids": [],
             "resource_ids": [1], "group_id": 0}
            for i in range(3)
        ],
    }
    manifest_path = out / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    command = build_gem5_cmd(
        topology="mesh", material=EXAMPLE_MATERIAL, bonding=EXAMPLE_BONDING,
        injection_rate=0.0, sim_cycles=100000, synthetic="uniform_random",
        outdir=str(out / "gem5"), num_cpus=16,
        sys_config=str(configs["system"]), net_config=str(configs["network"]),
        noc_config=str(configs["noc"]), noi_config=str(configs["noi"]),
        plc_config=str(configs["placement"]),
    )
    command.extend(("--buffers-per-ctrl-vc=16", "--buffers-per-data-vc=16", "--xor-low-bit=0"))
    with OnlineGem5Session(command, out / "gem5", prefer_reuse_encoding=bool(enabled)) as session:
        if engine == "cpp":
            response = session.run_cpp(
                manifest=str(manifest_path.resolve()), position=str(position.resolve()),
                catalog=str(catalog_path.resolve()), nonuniform_grid="", endpoint_mode="physical",
                k_node=2, k_chip=2, frequency_ghz=1.0,
                result_path=str((out / "result.json").resolve()),
            )
            result = json.loads(Path(response["result_path"]).read_text(encoding="utf-8"))
            annotate_endpoint_schedule(result, catalog, fixed=False)
        else:
            result = run_omelet_workload(
                manifest_path, OmeletNodeMap(position, k_node=2, k_chip=2), session, 1.0,
                prefer_reuse_encoding=bool(enabled),
            )
        assert session.in_flight() == 0
    assert result["message_arrived"] == 3, result
    assert all(p["state_final"] == "done" for p in result["phases"]), result
    assert [p["resource_ids"] for p in result["phases"][:2]] == (
        [[1], [0]] if enabled else [[0], [1]]
    ), result
    assert [p["compute_start_cycle"] for p in result["phases"][:2]] == [0, 0], result
    assert result["phases"][2]["compute_start_cycle"] >= 10, result
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"PASS engine={engine} encoding={enabled} resources="
          f"{[p['resource_ids'] for p in result['phases']]} arrived=3/3")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("cpp", "python", "both"), default="both")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "output" / "reuse_encoding_smoke"
    for engine in (("cpp", "python") if args.engine == "both" else (args.engine,)):
        for enabled in (0, 1):
            check(root / f"{engine}_{enabled}", engine, enabled)
