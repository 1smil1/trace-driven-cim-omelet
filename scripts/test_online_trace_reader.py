"""Exercise real C++ workload trace input, including a truncated record."""
import bz2
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parents[1]))
from chip.comm.online_workload_builder import _write_bz2_group
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import write_model_configs, EXAMPLE_MATERIAL, EXAMPLE_BONDING
from omelet.backends.gem5.online import OnlineGem5Session


def main():
    with tempfile.TemporaryDirectory(prefix="omelet_trace_") as temporary:
        out = Path(temporary)
        configs = write_model_configs(out / "config", k_chip=2, k_node=2,
                                     on_chip_width_bits=384, off_chip_serial_width_bits=2688)
        position = out / "positions.txt"
        rows = []
        for node in range(16):
            chiplet, local = divmod(node, 4)
            x = (chiplet % 2) * 2 + local % 2
            y = (chiplet // 2) * 2 + local // 2
            rows.append(f"{node} {x} {y} {chiplet} {chiplet} real {y * 4 + x}")
        position.write_text("\n".join(rows) + "\n", encoding="utf-8")
        for group, destination in enumerate((4, 8)):
            _write_bz2_group(str(out / f"group_{group}.bz2"),
                            [{"src": 0, "dst": destination, "size_bytes": 1024,
                              "shared_key": -1}], num_nodes=16)
        manifest = out / "manifest.json"
        manifest.write_text(json.dumps({
            "num_inputs": 1, "num_resources": 2,
            "template_groups": [{"group_id": i, "bz2_path": f"group_{i}.bz2",
                                 "shared_prefix_policy": 0} for i in range(2)],
            "phases": [{"phase_id": i, "input_id": 0, "layer_id": i,
                        "compute_latency_cycles": 10, "dep_phase_ids": [0] if i else [],
                        "resource_ids": [i], "group_id": i} for i in range(2)],
        }), encoding="utf-8")

        def run(name):
            output = out / name
            command = build_gem5_cmd(
                topology="mesh", material=EXAMPLE_MATERIAL, bonding=EXAMPLE_BONDING,
                injection_rate=0.0, sim_cycles=100000, synthetic="uniform_random",
                outdir=str(output), num_cpus=16,
                sys_config=str(configs["system"]), net_config=str(configs["network"]),
                noc_config=str(configs["noc"]), noi_config=str(configs["noi"]),
                plc_config=str(configs["placement"]),
            )
            command.extend(("--buffers-per-ctrl-vc=16", "--buffers-per-data-vc=16", "--xor-low-bit=0"))
            result = output / "result.json"
            with OnlineGem5Session(command, output) as session:
                response = session.run_cpp(manifest=str(manifest), position=str(position),
                    catalog="", nonuniform_grid="", endpoint_mode="physical",
                    k_node=2, k_chip=2, frequency_ghz=1.0, result_path=str(result))
                assert response["done"] and response["in_flight"] == 0
            progress = (output / "communication.log").read_text(encoding="utf-8")
            assert "stage=C++ online scheduler running" in progress
            assert "counters=last response" in progress
            assert "stage=C++ online scheduler completed" in progress
            assert "injected=2 arrived=2 pending=0 scheduler completed" in progress
            return json.loads(result.read_text(encoding="utf-8"))

        result = run("valid")
        assert result["message_injected"] == result["message_arrived"] == 2
        assert all(phase["state_final"] == "done" for phase in result["phases"])
        assert result["phases"][1]["compute_start_cycle"] >= result["phases"][0]["comm_end_cycle"]
        trace = out / "group_0.bz2"
        trace.write_bytes(bz2.compress(bz2.decompress(trace.read_bytes())[:-1]))
        try:
            run("truncated")
        except RuntimeError:
            logs = "".join((out / "truncated" / name).read_text(encoding="utf-8")
                           for name in ("gem5_stdout.log", "gem5_stderr.log"))
            assert "short netrace packet" in logs
        else:
            raise AssertionError("truncated packet was silently accepted")
        print("online trace reader OK: complete workload and truncated-record rejection")


if __name__ == "__main__":
    main()
