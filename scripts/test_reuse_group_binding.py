"""Real Garnet check: preserve X within Y, with explicit legacy borrowing."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re

from chip.comm.omelet_mapping import OmeletNodeMap
from chip.comm.omelet_scheduler import run_omelet_workload
from chip.comm.online_workload_builder import _write_bz2_group
from chip.comm.runtime_energy import annotate_endpoint_schedule
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import EXAMPLE_BONDING, EXAMPLE_MATERIAL, write_model_configs
from omelet.backends.gem5.online import OnlineGem5Session


def check(out, engine, encoding, borrowing):
    out.mkdir(parents=True, exist_ok=True)
    configs = write_model_configs(out / "config", k_chip=2, k_node=2)
    position = out / "positions.txt"
    positions = []
    for node in range(16):
        chip, local = divmod(node, 4)
        x, y = (chip % 2) * 2 + local % 2, (chip // 2) * 2 + local // 2
        positions.append(f"{node} {x} {y} 0 {chip} {chip} real {y * 4 + x}")
    position.write_text("\n".join(positions) + "\n", encoding="utf-8")
    layers = [
        {"layer_id": 0, "block_id": 0, "virtual_ids": [10], "canonical": [1], "candidates": [[0], [1]]},
        {"layer_id": 1, "block_id": -1, "virtual_ids": [20], "canonical": [3], "candidates": [[3]]},
        {"layer_id": 2, "block_id": 0, "virtual_ids": [11], "canonical": [1], "candidates": [[0], [1]]},
        {"layer_id": 3, "block_id": 0, "virtual_ids": [12], "canonical": [4], "candidates": [[2], [4]]},
    ]
    catalog = {
        "auto_reuse": 1, "resolve_mode": 1, "layers": layers,
        "virtual_tiles": [{"virtual_id": l["virtual_ids"][0], "physical_id": l["canonical"][0]} for l in layers],
        "physical_nodes": [{"physical_id": n, "tier_id": n//4} for n in range(16)],
    }
    (out / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    packets = [(10, 20), (11, 20), (12, 20), (20, 10)]
    for group, (src, dst) in enumerate(packets):
        _write_bz2_group(str(out / f"group_{group}.bz2"),
                         [{"src": src, "dst": dst, "size_bytes": 64, "shared_key": -1}],
                         debug=False)
    phases = []
    for i, (input_id, layer, resource, group, deps) in enumerate((
        (0, 0, 1, 0, []), (0, 2, 1, 1, []), (0, 3, 4, 2, []),
        (1, 0, 1, 0, []), (1, 1, 3, 3, [3]),
    )):
        phases.append({"phase_id": i, "input_id": input_id, "layer_id": layer,
                       "compute_latency_cycles": 10, "resource_ids": [resource],
                       "group_id": group, "dep_phase_ids": deps})
    manifest = out / "manifest.json"
    manifest.write_text(json.dumps({
        "num_inputs": 2, "num_resources": 16, "phases": phases,
        "endpoint_mapping_catalog": "catalog.json",
        "template_groups": [{"group_id": i, "bz2_path": f"group_{i}.bz2", "shared_prefix_policy": 0}
                            for i in range(4)],
    }), encoding="utf-8")
    command = build_gem5_cmd(
        topology="mesh", material=EXAMPLE_MATERIAL, bonding=EXAMPLE_BONDING,
        injection_rate=0.0, sim_cycles=100000, synthetic="uniform_random", num_cpus=16,
        outdir=str(out / "gem5"), sys_config=str(configs["system"]), net_config=str(configs["network"]),
        noc_config=str(configs["noc"]), noi_config=str(configs["noi"]), plc_config=str(configs["placement"]),
    )
    command.extend(("--buffers-per-ctrl-vc=16", "--buffers-per-data-vc=16", "--xor-low-bit=0"))
    # The C++ loop bypasses Python simulate's exit hook; explicitly dump only in this test.
    config_index = next(i for i, arg in enumerate(command) if Path(arg).name == "garnet_synth_traffic.py")
    original_config = Path(command[config_index]).resolve()
    probe_config = out / "dump_stats_config.py"
    probe_config.write_text(
        "import sys, runpy\nfrom pathlib import Path\n"
        f"config = (Path(__file__).parent / {Path(os.path.relpath(original_config, out)).as_posix()!r}).resolve()\n"
        "sys.path.insert(0, str(config.parent.parent))\n"
        "runpy.run_path(str(config), run_name='__main__')\n"
        "import m5\nm5.stats.dump()\n", encoding="utf-8")
    command[config_index] = str(probe_config.resolve())
    with OnlineGem5Session(command, out / "gem5", debug=True,
                           prefer_reuse_encoding=bool(encoding), allow_cross_group=bool(borrowing)) as session:
        if engine == "cpp":
            response = session.run_cpp(
                manifest=str(manifest.resolve()), position=str(position.resolve()), catalog=str((out / "catalog.json").resolve()),
                nonuniform_grid="", endpoint_mode="physical", k_node=2, k_chip=2, frequency_ghz=1.0,
                result_path=str((out / "result.json").resolve()),
            )
            result = json.loads(Path(response["result_path"]).read_text(encoding="utf-8"))
            annotate_endpoint_schedule(result, catalog, fixed=False, allow_cross_group=bool(borrowing))
        else:
            result = run_omelet_workload(
                manifest, OmeletNodeMap(position, k_node=2, k_chip=2), session, 1.0,
                prefer_reuse_encoding=bool(encoding), allow_cross_group=bool(borrowing),
            )
        assert session.in_flight() == 0
    rows = result["phases"]
    assert result["message_arrived"] == sum(g["packet_count"] for g in result["groups"]) == 5, result
    if engine == "cpp":
        assert result["message_injected"] == 5, result
    assert all(p["state_final"] == "done" for p in rows)
    first_pool = encoding
    assert rows[0]["resource_ids"] == [[0], [1]][first_pool]
    assert rows[2]["resource_ids"] == [[2], [4]][first_pool]
    assert rows[0]["compute_start_cycle"] == rows[2]["compute_start_cycle"] == 0
    if borrowing:
        assert rows[1]["resource_ids"] != rows[0]["resource_ids"]
        assert rows[1]["compute_start_cycle"] == 0
    else:
        assert rows[1]["resource_ids"] == rows[0]["resource_ids"]
        assert rows[1]["compute_start_cycle"] >= rows[0]["compute_end_cycle"]
        assert rows[3]["compute_start_cycle"] == 0
        assert rows[3]["resource_ids"] != rows[0]["resource_ids"]
    expected = Counter((rows[i]["resource_ids"][0], 3) for i in range(4))
    expected[(3, 1 if borrowing else rows[3]["resource_ids"][0])] += 1
    actual = {(int(s), int(d)): int(n) for s, d, n in re.findall(
        r"^system\.ruby\.network\.data_traffic_distribution\.n(\d+)::(\d+)\s+(\d+)",
        (out / "gem5" / "stats.txt").read_text(encoding="utf-8"), re.M)}
    assert actual == expected, (actual, expected)
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"PASS engine={engine} encoding={encoding} cross_group={borrowing} "
          f"resources={[r['resource_ids'] for r in rows]} starts={[r['compute_start_cycle'] for r in rows]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", choices=("cpp", "python", "both"), default="both")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "output" / "reuse_group_binding"
    for engine in (("cpp", "python") if args.engine == "both" else (args.engine,)):
        for encoding in (0, 1):
            for borrowing in (0, 1):
                check(root / f"{engine}_e{encoding}_b{borrowing}", engine, encoding, borrowing)
