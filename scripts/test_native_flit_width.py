"""Check native port-sized flits and configured clocks in built gem5."""
from configparser import ConfigParser
from pathlib import Path
import json
import re
import sys
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from omelet.backends.gem5.adapter import build_gem5_cmd
from omelet.backends.gem5.model_config import write_model_configs
from omelet.backends.gem5.online import OnlineGem5Session


def main():
    results = []
    for width, quantum, noi_ghz in ((128, 128, 1.0), (384, 128, 1.0), (384, 64, 0.5)):
        out = ROOT / "output" / "native_flit_width" / f"width{width}_quantum{quantum}_noi{noi_ghz:g}"
        configs = write_model_configs(out / "config", k_chip=2, k_node=2,
            flit_bits=quantum, on_chip_width_bits=width,
            off_chip_serial_width_bits=8192, clock_ghz=1.0)
        system = yaml.safe_load(configs["system"].read_text(encoding="utf-8"))
        system["noi_clock"] = f"{noi_ghz:g}GHz"
        configs["system"].write_text(yaml.safe_dump(system), encoding="utf-8")
        cmd = build_gem5_cmd(topology="mesh", material="org", injection_rate=0.0,
            sim_cycles=10000, synthetic="uniform_random", outdir=str(out), num_cpus=16,
            sys_config=str(configs["system"]), net_config=str(configs["network"]),
            noc_config=str(configs["noc"]), noi_config=str(configs["noi"]),
            plc_config=str(configs["placement"]))
        cmd.extend(["--vcs-per-vnet=4", "--buffers-per-data-vc=16", "--buffers-per-ctrl-vc=16"])
        with OnlineGem5Session(cmd, out, debug=True) as session:
            session.inject(0, 15, 64, 1)
            arrivals = session.advance(1_000_000)["arrivals"]
            assert len(arrivals) == 1 and session.in_flight() == 0, arrivals
            # A same-chiplet packet can exceed VC depth without packet splitting.
            session.inject(0, 1, 1024, 2)
            long_arrivals = session.advance(1_000_000)["arrivals"]
            assert len(long_arrivals) == 1 and session.in_flight() == 0, long_arrivals
            arrivals.extend(long_arrivals)
        stats = (out / "stats.txt").read_text(encoding="utf-8")
        counts = {}
        for metric in ("flits_injected", "flits_received", "packets_received"):
            match = re.search(r"^system\.ruby\.network\." + metric + r"::total\s+(\d+)", stats, re.M)
            assert match, metric
            counts[metric] = int(match.group(1))
        expected_flits = (64 * 8 + width - 1) // width + (1024 * 8 + width - 1) // width
        assert counts == {"flits_injected": expected_flits,
            "flits_received": expected_flits, "packets_received": 2}, counts
        config = ConfigParser()
        config.read(out / "config.ini", encoding="utf-8")
        assert int(config["system.ruby.network"]["ni_flit_size"]) == width // 8
        clocked_objects = 0
        for name in config.sections():
            obj = config[name]
            if "type" not in obj or obj["type"] not in (
                "GarnetRouter", "GarnetNetworkInterface", "NetworkLink", "CreditLink", "NetworkBridge"):
                continue
            clock = int(config[obj["clk_domain"]]["clock"])
            assert clock in (1000, int(1000 / noi_ghz)), (name, obj["type"], clock)
            if obj["type"] == "GarnetRouter":
                expected_width = width if obj["role"].startswith("chiplet") else 8192
                assert int(obj["width"]) == expected_width, (name, obj["width"])
                expected_clock = 1000 if obj["role"].startswith("chiplet") else int(1000 / noi_ghz)
                assert clock == expected_clock, (name, clock, expected_clock)
            if obj["type"] == "GarnetNetworkInterface":
                assert clock == 1000, (name, clock)
            clocked_objects += 1
        assert clocked_objects > 0
        results.append({"onchip_width_bits": width, "noi_width_bits": 8192,
            "bandwidth_quantum_bits": quantum, "packet_bytes": [64, 1024],
            "noc_clock_ps": 1000, "noi_clock_ps": int(1000 / noi_ghz),
            "vc_buffer_depth": 16, "clocked_objects_checked": clocked_objects,
            "arrivals": arrivals, **counts})
        print(json.dumps(results[-1]), flush=True)
    (ROOT / "output" / "native_flit_width" / "results.json").write_text(
        json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
