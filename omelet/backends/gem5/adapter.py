
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING
import yaml

from omelet.backends.base import BackendAdapter, SimPoint

if TYPE_CHECKING:
    from omelet.nox.graph import NoXGraph



_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_CONFIGS   = _REPO_ROOT / "omelet" / "techlib"
_EXAMPLES  = _REPO_ROOT / "examples"


def _resolve_gem5_dir() -> str:
    # Environment is an explicit override; local gem5 is the documented default.
    if 'OMELET_GEM5_DIR' in os.environ:
        value = os.environ['OMELET_GEM5_DIR']
        if not value.strip():
            raise ValueError('OMELET_GEM5_DIR must not be empty')
        return value
    return str(_REPO_ROOT / 'gem5')


_GEM5_DIR  = _resolve_gem5_dir()

_DEFAULT_GEM5_OPT = str(Path(_GEM5_DIR) / "build" / "Garnet_standalone" / (
    "gem5.opt.exe" if os.name == "nt" else "gem5.opt"
))
_GARNET_CFG    = str(Path(_GEM5_DIR) / "configs" / "example" / "garnet_synth_traffic.py")


def _gem5_binary() -> str:
    return _DEFAULT_GEM5_OPT


def _noi_file(topology: str) -> str:
    mapping = {
        "mesh":   "meshnoi.yaml",
        "irregular": "meshnoi.yaml",
    }
    if topology not in mapping:
        raise ValueError(f"Unsupported Omelet topology: {topology}")
    return mapping[topology]



def build_gem5_cmd(
    *,
    topology: str,
    material: str,
    injection_rate: float,
    sim_cycles: int,
    synthetic: str,
    outdir: str,
    num_cpus: int = 64,
    sys_config: str | None = None,
    net_config: str | None = None,
    noc_config: str | None = None,
    noi_config: str | None = None,
    plc_config: str | None = None,
    nol_config: str | None = None,
    bonding: str | None = None,
    c2c_bonding: str | None = None,
    topology_flag: str | None = None,
    nonuniform_grid_config: str | None = None,
    vertical_interconnect_method: int | None = None,
) -> list[str]:
    gem5 = _gem5_binary()
    noi  = _noi_file(topology)

    sys_cfg = sys_config or str(_EXAMPLES / "system" / "system_2_5d.yaml")
    noc_cfg = noc_config or str(_EXAMPLES / "network" / "noc.yaml")
    noi_cfg = noi_config or str(_EXAMPLES / "network" / noi)
    plc_cfg = plc_config or str(_EXAMPLES / "placement" / "place_model_2_5d.yaml")
    topo_flag = topology_flag or f"{topology}_nox"
    system = yaml.safe_load(Path(sys_cfg).read_text(encoding="utf-8"))
    reference_clock = system["chiplet_clock"]

    cmd = [
        gem5,
        f"--outdir={outdir}",
        _GARNET_CFG,
        f"--num-cpus={num_cpus}",
        f"--num-dirs={num_cpus}",
        f"--sys-clock={reference_clock}",
        f"--ruby-clock={reference_clock}",
        f"--sim-cycles={sim_cycles}",
        "--network=garnet",
        f"--topology={topo_flag}",
        f"--synthetic={synthetic}",
        f"--injectionrate={injection_rate}",
        f"--sys_config={sys_cfg}",
        f"--noc_config={noc_cfg}",
        f"--noi_config={noi_cfg}",
        f"--material={material}",
        f"--plc_config={plc_cfg}",
    ]

    if net_config:
        cmd.append(f"--net_config={net_config}")
    if nol_config:
        cmd.append(f"--nol_config={nol_config}")
    if nonuniform_grid_config:
        cmd.append(f"--nonuniform_grid_config={nonuniform_grid_config}")
    if bonding:
        cmd.append(f"--bonding={bonding}")
    if c2c_bonding:
        cmd.append(f"--c2c_bonding={c2c_bonding}")
    if vertical_interconnect_method is not None:
        if type(vertical_interconnect_method) is not int or vertical_interconnect_method not in (0,1):
            raise ValueError('vertical_interconnect_method must be 0 or 1')
        cmd.append(f'--vertical-interconnect-method={vertical_interconnect_method}')
    return cmd



def _cygpath_u(msys_bin: Path, path: Path) -> str:
    """cygpath -u with retries for transient Windows DLL-init failures.

    Under commit-memory pressure cygpath.exe can die before its DLLs load
    (0xC0000142, exit 3221225794). The failure is transient, so retry with
    a backoff instead of failing the whole gem5 launch; the final failure
    still raises with the original context.
    """
    last_error: Exception | None = None
    for attempt in range(4):
        if attempt:
            time.sleep(20 * attempt)
        try:
            return subprocess.run(
                [str(msys_bin / "cygpath.exe"), "-u", str(path)],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        except subprocess.CalledProcessError as exc:
            last_error = exc
    raise RuntimeError(
        f"MSYS2 path conversion failed after 4 attempts for {path}"
    ) from last_error


def prepare_gem5_process(cmd: list[str]) -> tuple[list[str], dict[str, str] | None]:
    gem5_bin = Path(cmd[0])
    if not gem5_bin.is_file():
        raise FileNotFoundError(
            f"gem5 binary not found: {gem5_bin}\n"
            "Build the tree first or set OMELET_GEM5_DIR."
        )

    Path(cmd[1].split('=', 1)[1]).mkdir(parents=True, exist_ok=True)

    env = None
    if os.name == "nt":
        msys_bin = Path(os.environ.get("OMELET_MSYS2_BIN", r"C:\msys64\usr\bin"))
        if not (msys_bin / "msys-2.0.dll").is_file():
            raise FileNotFoundError(f"MSYS2 runtime not found in {msys_bin}; set OMELET_MSYS2_BIN")
        cygpath = msys_bin / "cygpath.exe"
        if not cygpath.is_file():
            raise FileNotFoundError(f"MSYS2 path converter not found: {cygpath}")
        for index, arg in enumerate(cmd):
            if index == 2:
                path = arg
                prefix = ""
            elif arg.startswith(("--outdir=", "--sys_config=", "--noc_config=", "--noi_config=", "--plc_config=", "--net_config=", "--nol_config=", "--nonuniform_grid_config=")):
                prefix, path = arg.split("=", 1)
                prefix += "="
            else:
                continue
            converted = _cygpath_u(msys_bin, Path(path).resolve())
            if not converted:
                raise RuntimeError(f"MSYS2 path conversion returned empty result for {path}")
            cmd[index] = prefix + converted
        env = os.environ.copy()
        env["PATH"] = str(msys_bin) + os.pathsep + env["PATH"]
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
        tools_dir = _REPO_ROOT / ".tools"
        if not (tools_dir / "yaml" / "__init__.py").is_file():
            raise FileNotFoundError(f"Omelet gem5 Python dependencies missing in {tools_dir}; run install_windows.ps1")
        repo_path = _cygpath_u(msys_bin, _REPO_ROOT)
        tools_path = _cygpath_u(msys_bin, tools_dir)
        env["PYTHONPATH"] = tools_path + ":" + repo_path
    return cmd, env


class Gem5Adapter(BackendAdapter):

    name: str = "gem5"

    def run(self, sim_spec: SimPoint) -> Path:
        cmd = build_gem5_cmd(
            topology=sim_spec.topology,
            material=sim_spec.material,
            injection_rate=sim_spec.injection_rate,
            sim_cycles=sim_spec.sim_cycles,
            synthetic=sim_spec.synthetic,
            outdir=sim_spec.outdir,
            num_cpus=sim_spec.num_cpus,
            sys_config=sim_spec.sys_config,
            net_config=sim_spec.net_config,
            noc_config=sim_spec.noc_config,
            noi_config=sim_spec.noi_config,
            plc_config=sim_spec.plc_config,
            nol_config=sim_spec.nol_config,
            bonding=sim_spec.bonding,
            c2c_bonding=sim_spec.c2c_bonding,
            vertical_interconnect_method=sim_spec.vertical_interconnect_method,
            topology_flag=sim_spec.topology_flag,
        )

        cmd, env = prepare_gem5_process(cmd)
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env)
        if result.returncode != 0:
            stderr_lines = result.stderr.decode(errors="replace").strip().splitlines()
            tail = "\n".join(stderr_lines[-20:]) if stderr_lines else "(no stderr)"
            raise RuntimeError(
                f"gem5 exited with code {result.returncode}\n"
                f"--- gem5 stderr (last 20 lines) ---\n{tail}"
            )

        stats_path = Path(sim_spec.outdir) / "stats.txt"
        if not stats_path.is_file() or stats_path.stat().st_size == 0:
            raise RuntimeError(f"gem5 produced no nonempty stats.txt in {sim_spec.outdir}")

        return stats_path.resolve()

    def emit(self, graph: "NoXGraph") -> str:
        tiers = sorted({r.tier for r in graph.routers})
        tier_str = "+".join(tiers) if tiers else "unknown"

        lines: list[str] = [
            f"# gem5/Garnet topology realization",
            f"# NOTE: authoritative construction is in-process via omelet.nox",
            f"#       (configs/topologies/<topo>_nox.py). This is the backend-",
            f"#       agnostic view only.",
            f"",
            f"topology_spec : <topo>_nox  (gem5 --topology flag suffix '_nox')",
            f"tiers         : {tier_str}",
            f"routers       : {len(graph.routers)}",
            f"nodes         : {len(graph.nodes)}",
            f"links         : {len(graph.links)}",
            f"routing       : {graph.routing}",
            f"",
            f"link_summary (src -> dst | layer | width_b | lat_cyc):",
        ]

        for lk in graph.links:
            lines.append(
                f"  {lk.src:>10} -> {lk.dst:<10}  "
                f"{lk.layer:<4}  "
                f"W={lk.tech.width:>4}b  "
                f"t={lk.tech.latency_cycles}cyc"
            )

        return "\n".join(lines)
