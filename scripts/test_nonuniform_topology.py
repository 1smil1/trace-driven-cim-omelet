"""Run mixed-size chiplets, including a transit-only empty slot, in gem5."""
import argparse
import json
import subprocess
import yaml
import sys
from pathlib import Path

OMELET = Path(__file__).resolve().parents[1]
ROOT = OMELET.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(OMELET))

from chip.comm.online_workload_builder import OnlineWorkloadBuilder
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import write_model_configs, EXAMPLE_MATERIAL, EXAMPLE_BONDING
from omelet.backends.gem5.online import OnlineGem5Session
from chip.comm.network_options import add_network_arguments, network_options


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_network_arguments(parser)
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--rectangular", action="store_true", help="5x6 slots, only opposite corners carry traffic")
    parser.add_argument("--stack-layers", type=int, default=1)
    parser.add_argument("--packet-bytes", type=int, default=1024)
    parser.add_argument("--buffer-depth", type=int, default=16)
    parser.add_argument("--off-chip-width-bits", type=int, default=128)
    parser.add_argument("--on-chip-width-bits", type=int, default=128)
    parser.add_argument("--backend", choices=("omelet", "cnsim"), default="omelet")
    parser.add_argument("--output-dir", type=Path, default=OMELET / "output/nonuniform_smoke")
    args = parser.parse_args()
    tuning = network_options(args)
    if args.backend == "cnsim" and tuning:
        parser.error("Omelet physics overrides require --backend omelet")
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    grids = {
        "0": {"tier_ids": [0], "chiplet_pos": [0, 0], "origin": [0, 0], "grid": [2, 2], "node_id_base": 0},
        "1": {"tier_ids": [1], "chiplet_pos": [1, 0], "origin": [2, 0], "grid": [4, 4], "node_id_base": 4},
        "2": {"tier_ids": [2], "chiplet_pos": [0, 1], "origin": [0, 4], "grid": [2, 2], "node_id_base": 20},
        "3": {"tier_ids": [], "chiplet_pos": [1, 1], "origin": [2, 4], "grid": [4, 2], "node_id_base": 24},
    }
    if args.rectangular:
        grids = {}
        base = 0
        for slot in range(30):
            x, y = slot % 5, slot // 5
            side = 4 if slot == 29 else 2
            grids[str(slot)] = {"tier_ids": [slot] if slot in (0, 29) else [],
                                "chiplet_pos": [x, y], "origin": [x * 4, y * 4],
                                "grid": [side, side], "node_id_base": base}
            base += side * side
    if args.stack_layers > 1:
        grids = {str(i): {"chiplet_pos": [i % 2, 0, i // 2],
                         "origin": [(i % 2) * 2, 0, i // 2],
                         "grid": [2, 2], "node_id_base": i * 4}
                 for i in range(2 * args.stack_layers)}
    router_count = sum(meta["grid"][0] * meta["grid"][1] for meta in grids.values())
    grid_path = OnlineWorkloadBuilder("GPT2")._write_nonuniform_tier_grid_config(str(out), chiplets=grids)
    data = json.loads(grid_path.read_text(encoding="utf-8"))
    assert data["grid_shape"] == ([2, 1, args.stack_layers] if args.stack_layers > 1 else [5, 6, 1] if args.rectangular else [2, 2, 1])
    pairs = {(row["src_chiplet"], row["dst_chiplet"]) for row in data["links"]}
    expected = {(s, s + 1) for s in range(30) if s % 5 < 4} | {(s, s + 5) for s in range(25)} if args.rectangular else {(0, 1), (0, 2), (1, 3), (2, 3)}
    assert pairs == ({(0, 1)} if args.stack_layers > 1 else expected)
    cfg = write_model_configs(out / "config", k_chip=2, k_node=2, nonuniform_grid_file=grid_path,
                              off_chip_serial_width_bits=args.off_chip_width_bits,
                              on_chip_width_bits=args.on_chip_width_bits, network_options=tuning)
    network_config = yaml.safe_load(cfg["network"].read_text(encoding="utf-8"))
    assert network_config["onchip_width"] == args.on_chip_width_bits
    if args.off_chip_width_bits:
        from omelet.nox.builders import noi_router_width
        assert network_config["offchip_width"] == args.off_chip_width_bits
        assert noi_router_width(None, None, network_config, None) == args.off_chip_width_bits
    if args.config_only:
        print("Mixed grids and empty-slot boundary links validated")
        return
    pairs_to_test = [(0, 3), (0, 19), (19, 20), (20, 0)]
    if args.stack_layers > 1:
        top = (args.stack_layers - 1) * 8
        pairs_to_test = [(0, top), (top, 0), (top, top + 7), (top + 7, top)]
    if args.rectangular:
        last = grids["29"]["node_id_base"] + 15
        pairs_to_test = [(0, last), (last, 0)]
    if args.backend == "cnsim":
        if args.on_chip_width_bits != 128:
            raise ValueError("CNSim smoke currently requires on-chip width 128 bits")
        from chip.comm.online_workload_builder import IniBuilder, _write_bz2_group
        if args.off_chip_width_bits <= 0 or args.off_chip_width_bits % 128:
            raise ValueError("CNSim smoke requires a positive off-chip width divisible by 128 bits")
        count = len(pairs_to_test)
        manifest = {"num_inputs": 1, "num_resources": 1,
                    "packet_templates": [{"packet_id": i, "src_node": src, "dst_node": dst,
                                          "size_bytes": args.packet_bytes} for i, (src, dst) in enumerate(pairs_to_test)],
                    "comm_tasks": [{"task_id": i, "packet_begin": i, "packet_end": i + 1} for i in range(count)],
                    "phases": [{"phase_id": i, "input_id": 0, "layer_id": i, "compute_latency_cycles": 1,
                                "dep_phase_ids": [i - 1] if i else [], "resource_ids": [0],
                                "comm_task_begin": i, "comm_task_end": i + 1} for i in range(count)]}
        manifest.pop("packet_templates")
        manifest.pop("comm_tasks")
        manifest["template_groups"] = []
        for i, (src, dst) in enumerate(pairs_to_test):
            name = f"group_{i}.bz2"
            _write_bz2_group(str(out / name), [{"src": src, "dst": dst,
                                              "size_bytes": args.packet_bytes, "shared_key": -1}],
                             num_nodes=router_count)
            manifest["template_groups"].append({"group_id": i, "bz2_path": name})
            manifest["phases"][i].pop("comm_task_begin")
            manifest["phases"][i].pop("comm_task_end")
            manifest["phases"][i]["group_id"] = i
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        ini = IniBuilder().build(manifest_path="manifest.json", output_csv_path="cnsim.csv",
                                 log_path="cnsim.log", buffer_size=args.buffer_depth, vc_number=4,
                                 on_chip_width_flits=1, off_chip_serial_width_flits=args.off_chip_width_bits // 128,
                                 nonuniform_tier_grid_file=grid_path.name, online_debug=True)
        width, height = data["grid_shape"]
        ini = ini.replace("[Network]\n", f"[Network]\nchip_w = {width}\nchip_h = {height}\n", 1)
        ini = ini.replace("pause_on_first_injection = true", "pause_on_first_injection = false")
        ini = ini.replace("pause_on_input_done = true", "pause_on_input_done = false")
        (out / "sim_config.ini").write_text(ini, encoding="utf-8")
        exe = ROOT / "chip/chiplet-network-sim/ChipletNetworkSim.exe"
        if sys.platform != "win32":
            exe = ROOT / "chip/chiplet-network-sim/ChipletNetworkSim"
        with (out / "cnsim_stdout.log").open("w", encoding="utf-8") as log:
            subprocess.run([str(exe), "sim_config.ini"], cwd=out, stdout=log, stderr=subprocess.STDOUT,
                           timeout=60, check=True)
        result = json.loads((out / "cnsim.json").read_text(encoding="utf-8"))
        assert len(result["groups"]) == count, result
        assert all(int(phase["comm_end_cycle"]) > int(phase["comm_start_cycle"]) for phase in result["phases"]), result
        print(json.dumps(result, indent=2))
        return
    cmd = build_gem5_cmd(
        topology="irregular", material=tuning.get("interposer_material", EXAMPLE_MATERIAL), bonding=tuning.get("bonding", EXAMPLE_BONDING),
        injection_rate=0, sim_cycles=100000, synthetic="uniform_random",
        outdir=str(out / "gem5"), num_cpus=router_count,
        sys_config=str(cfg["system"]), net_config=str(cfg["network"]),
        noc_config=str(cfg["noc"]), noi_config=str(cfg["noi"]), plc_config=str(cfg["placement"]),
        nonuniform_grid_config=str(grid_path),
    )
    ctrl = tuning.get("control_buffer_depth", args.buffer_depth)
    cmd.append("--c2c_bonding=" + tuning.get("c2c_bonding", "ubump10"))
    data_depth = tuning.get("data_buffer_depth", args.buffer_depth)
    cmd.extend([f"--buffers-per-ctrl-vc={ctrl}", f"--buffers-per-data-vc={data_depth}", "--xor-low-bit=0", "--routing-algorithm=0"])
    results = []
    with OnlineGem5Session(cmd, out / "gem5") as session:
        session.advance(1)
        for packet_id, (src, dst) in enumerate(pairs_to_test, 1):
            start = session.inject(src, dst, args.packet_bytes, packet_id)
            arrivals = []
            for _ in range(100):
                response = session.advance(1000000)
                arrivals.extend(response["arrivals"])
                if arrivals:
                    break
            assert len(arrivals) == 1 and arrivals[0]["id"] == packet_id, response
            assert session.in_flight() == 0
            results.append({"src": src, "dst": dst, "latency_ns": (arrivals[0]["tick"] - start) / 1000})
    (out / "result.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
