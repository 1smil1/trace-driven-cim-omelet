"""Expand Omelet's 2.5D example parameters to a model-derived square chiplet grid."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import yaml

from omelet.placement.engine import Chiplet, PlaceMap
from omelet.nox.concentration import concentrated_regions
from omelet.inputs.coordinates import normalize_grid


_EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
EXAMPLE_MATERIAL = "org"
EXAMPLE_BONDING = "ubump30"

# Above this chiplet count, placement distances keep only same-row /
# same-column pairs (collinear_only) instead of the full N*(N-1)/2 table.
# The gem5 runtime consumes distances solely through chiplet_beachfront_um()
# and stack_overlap_um2() in omelet.nox.builders; side_overlap is positive
# exactly for collinear same-tier pairs, so the derived scalars are identical.
# A degenerate 1xN / Nx1 grid degrades collinear_only back to O(n^2) pairs —
# bounded and still far below the full-table memory cost per pair.
_FULL_DISTANCES_MAX_CHIPLETS = 256


def write_model_configs(outdir: str | Path, *, k_chip: int, k_node: int, vc_number: int = 4, on_chip_width_flits: int = 1, off_chip_serial_width_flits: int = 0, flit_bits: int = 128, on_chip_width_bits: int | None = None, off_chip_serial_width_bits: int | None = None, clock_ghz: float = 1.0, nonuniform_grid_file: str | Path | None = None, full_distances_max_chiplets: int | None = None, network_options: dict | None = None) -> dict[str, Path]:
    """Write native Garnet widths; flit_bits quantizes derived bandwidth only.

    Actual flit widths follow each router/link port and native SerDes conversion.
    Legacy width_flits arguments are bit-width multipliers, not multiple Garnet
    flits sent in one cycle.
    """
    if type(k_chip) is not int or k_chip <= 0 or type(k_node) is not int or k_node <= 0:
        raise ValueError("k_chip and k_node must be positive integers")
    if min(vc_number, on_chip_width_flits) <= 0 or off_chip_serial_width_flits < 0:
        raise ValueError("network widths and VC count must be positive; off-chip width may be zero for auto")
    if flit_bits <= 0:
        raise ValueError("flit_bits must be positive")
    on_chip_width_bits = int(on_chip_width_bits if on_chip_width_bits is not None else on_chip_width_flits * flit_bits)
    off_chip_serial_width_bits = int(off_chip_serial_width_bits if off_chip_serial_width_bits is not None else off_chip_serial_width_flits * flit_bits)
    if on_chip_width_bits <= 0 or off_chip_serial_width_bits < 0 or on_chip_width_bits % flit_bits or off_chip_serial_width_bits % flit_bits:
        raise ValueError("link widths must be non-negative multiples of flit_bits")
    on_chip_width_flits = on_chip_width_bits // flit_bits
    off_chip_serial_width_flits = off_chip_serial_width_bits // flit_bits
    if not math.isfinite(clock_ghz) or clock_ghz <= 0:
        raise ValueError("clock_ghz must be positive")
    tuning = {} if network_options is None else dict(network_options)
    allowed = set("noi_concentration interposer_material bonding noc_freq_ghz noi_freq_ghz noc_router_latency noi_router_latency ext_link_latency control_buffer_depth data_buffer_depth noi_link_length_mm serdes_latency serdes_bridge_latency cdc_latency c2c_bonding via_signal_area_mm2 vertical_interconnect_method".split())
    if set(tuning) - allowed:
        raise ValueError(f"Unknown network parameters: {sorted(set(tuning) - allowed)}")
    for key, value in tuning.items():
        if key in ("interposer_material", "bonding", "c2c_bonding"):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid {key}: {value}")
        if key not in ("serdes_latency", "serdes_bridge_latency", "cdc_latency", "vertical_interconnect_method") and value == 0:
            raise ValueError(f"{key} must be positive")
        if key not in ("noc_freq_ghz", "noi_freq_ghz", "noi_link_length_mm", "via_signal_area_mm2") and type(value) is not int:
            raise ValueError(f"{key} must be an integer")
    if "noi_link_length_mm" in tuning and not 1 <= tuning["noi_link_length_mm"] <= 6:
        raise ValueError("NoI length table covers 1..6 mm")
    material = tuning.get("interposer_material", EXAMPLE_MATERIAL)
    bonding = tuning.get("bonding", EXAMPLE_BONDING)
    from omelet.techlib import load_latency_table, load_epb_table
    from omelet.linkmodel.width import signal_pitch_um
    signal_pitch_um(bonding, material)
    load_latency_table(material, bonding)
    if load_epb_table(material, bonding) is None:
        raise ValueError(f"No EPB table for {material}/{bonding}")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    grid_data = None
    if nonuniform_grid_file is not None:
        grid_path = Path(nonuniform_grid_file)
        grid_data = normalize_grid(json.loads(grid_path.read_text(encoding="utf-8")))
        if grid_data["enabled"] is not True or not isinstance(grid_data["chiplets"], dict):
            raise ValueError(f"invalid nonuniform grid file: {grid_path}")
        chiplet_ids = sorted((int(key) for key in grid_data["chiplets"]), key=int)
        if chiplet_ids != list(range(len(chiplet_ids))):
            raise ValueError("nonuniform chiplet IDs must include contiguous transit slots")
        chiplets = len(chiplet_ids)
        routers = 0
        for chiplet_id in chiplet_ids:
            shape = grid_data["chiplets"][str(chiplet_id)]["grid"]
            if len(shape) != 2 or int(shape[0]) <= 0 or int(shape[1]) <= 0:
                raise ValueError(f"invalid grid for chiplet {chiplet_id}")
            routers += int(shape[0]) * int(shape[1])
    else:
        chiplets = k_chip * k_chip
        routers = chiplets * k_node * k_node

    layers = 1 if grid_data is None else grid_data["grid_shape"][2]
    if layers > 1:
        bottom = grid_data["grid_shape"][0] * grid_data["grid_shape"][1]
        bottom_grid = {**grid_data, "grid_shape": [*grid_data["grid_shape"][:2], 1],
                       "chiplets": {str(i): grid_data["chiplets"][str(i)] for i in range(bottom)}}
    else:
        bottom_grid = grid_data
    sources = {
        "system": _EXAMPLES / "system/system_2_5d.yaml",
        "network": _EXAMPLES / "network/network_2_5d.yaml",
        "noc": _EXAMPLES / "network/noc.yaml",
        "noi": _EXAMPLES / "network/meshnoi.yaml",
    }
    configs = {name: yaml.safe_load(path.read_text(encoding="utf-8")) for name, path in sources.items()}
    configs["system"]["clock_ghz"] = float(clock_ghz)
    configs["system"]["chiplet_clock"] = f"{clock_ghz:g}GHz"
    configs["system"]["noi_clock"] = f"{clock_ghz:g}GHz"
    configs["system"]["mem_clock"] = f"{clock_ghz:g}GHz"
    configs["system"]["latency_quantum_ps"] = 1000.0 / float(clock_ghz)
    for hierarchy, clock in (("noc", "chiplet_clock"), ("noi", "noi_clock")):
        key = hierarchy + "_freq_ghz"
        if key in tuning:
            configs["system"][clock] = f"{tuning[key]:g}GHz"
            configs["system"][hierarchy + "_clock_ghz"] = tuning[key]
    net = configs["network"]
    from omelet.nox.vertical import resolve_vertical_method
    net['vertical_interconnect_method'] = resolve_vertical_method(grid_data, tuning.get('vertical_interconnect_method',0))
    if int(net["flit_bits"]) != int(flit_bits):
        net["flit_bits"] = int(flit_bits)
    net["onchip_width"] = on_chip_width_bits
    net["top_vc"] = int(vc_number)
    if off_chip_serial_width_bits:
        net["offchip_width"] = off_chip_serial_width_bits
    if grid_data is None:
        net["noc_routers_per_chiplet"] = {f"chiplet{i}": k_node * k_node for i in range(chiplets)}
        net["cores_per_chiplet"] = {f"chiplet{i}": k_node * k_node for i in range(chiplets)}
    else:
        net["noc_routers_per_chiplet"] = {
            f"chiplet{i}": int(grid_data["chiplets"][str(i)]["grid"][0]) * int(grid_data["chiplets"][str(i)]["grid"][1])
            for i in range(chiplets)
        }
        net["cores_per_chiplet"] = dict(net["noc_routers_per_chiplet"])
        net["nonuniform_grid_file"] = str(Path(nonuniform_grid_file).resolve())
    latency = net["router_latency"]["chiplets"]["chiplet0"]
    net["router_latency"]["chiplets"] = {f"chiplet{i}": latency for i in range(chiplets)}
    configs["noc"]["mesh_rows"] = k_node
    configs["noi"]["num_routers"] = routers if grid_data is None else len(bottom_grid["chiplets"])
    configs["noi"]["mesh_rows"] = k_chip * k_node if grid_data is None else int(grid_data["grid_shape"][1])
    if "noi_concentration" in tuning:
        concentration = tuning["noi_concentration"]
        side = math.isqrt(concentration)
        if side == 0 or side * side != concentration:
            raise ValueError("NoI concentration must be a positive square")
        configs["noi"]["router"]["concentration"] = concentration
        if grid_data is None:
            if k_node % side:
                raise ValueError("Uniform NoC size must divide the concentration side; use nonuniform mapping for partial blocks")
            configs["noi"]["num_routers"] = routers // concentration
            configs["noi"]["mesh_rows"] = k_chip * k_node // side
        else:
            configs["noi"]["concentrated_mapping"] = True
            _, configs["noi"]["num_routers"] = concentrated_regions(bottom_grid, concentration)
    if "noc_router_latency" in tuning:
        net["router_latency"]["chiplets"] = {f"chiplet{i}": tuning["noc_router_latency"] for i in range(chiplets)}
    if "noi_router_latency" in tuning:
        net["router_latency"]["interposer"] = tuning["noi_router_latency"]
    if "ext_link_latency" in tuning:
        net["ext_latency"] = tuning["ext_link_latency"]
    if "noi_link_length_mm" in tuning:
        net["noi_link_length_override_m"] = tuning["noi_link_length_mm"] * 1e-3
    for key in ("serdes_latency", "serdes_bridge_latency", "cdc_latency"):
        if key in tuning:
            net[key + "_override"] = tuning[key]

    if layers > 1:
        net["stack_layers"] = layers
        from omelet.techlib import load_3d_latency, load_3d_epb
        from omelet.linkmodel.width import BOND_PITCH_UM
        from omelet.nox.vertical import vertical_ports
        vertical_bond = tuning.get("c2c_bonding", "ubump10")
        load_3d_latency(vertical_bond)
        load_3d_epb(vertical_bond)
        if "via_signal_area_mm2" in tuning:
            resolved_via_area = float(tuning["via_signal_area_mm2"])
        else:
            # Method 1 gives every mapped vertical port one Garnet width
            # quantum.  Resolve the omitted default to the smallest legal
            # signal area for this stack instead of creating an invalid
            # configuration that fails later inside gem5 initialization.
            pitch_um = float(BOND_PITCH_UM[vertical_bond])
            required_um2 = 0.0
            if int(net["vertical_interconnect_method"]) == 1:
                plane = int(grid_data["grid_shape"][0]) * int(grid_data["grid_shape"][1])
                for upper in range(plane, len(grid_data["chiplets"])):
                    lower = upper - plane
                    lower_grid = grid_data["chiplets"][str(lower)]["grid"]
                    upper_grid = grid_data["chiplets"][str(upper)]["grid"]
                    port_count = len(vertical_ports(lower_grid, upper_grid, 1))
                    required_um2 = max(
                        required_um2,
                        float(port_count * int(flit_bits)) * pitch_um * pitch_um,
                    )
            resolved_via_area = max(0.1, required_um2 / 1.0e6)
        configs["system"]["via_signal_area_mm2"] = resolved_via_area
    example_place = yaml.safe_load(
        (_EXAMPLES / "placement/place_model_2_5d.yaml").read_text(encoding="utf-8")
    )
    example_chip = example_place["chiplets"]["chiplet0"]
    place = PlaceMap(
        pitch_m=example_place["pitch_m"],
        chiplet_keepout=example_place["chiplet_keepout"],
        stack=layers,
    )
    if grid_data is None:
        place.rowscols(k_chip, k_chip)
    for chiplet_id in range(chiplets):
        if grid_data is None:
            chip_x, chip_y = chiplet_id % k_chip, chiplet_id // k_chip
            chip_z = 0
            shape = (k_node, k_node)
        else:
            meta = grid_data["chiplets"][str(chiplet_id)]
            chip_x, chip_y = (int(meta["chiplet_pos"][0]), int(meta["chiplet_pos"][1]))
            shape = (int(meta["grid"][0]), int(meta["grid"][1]))
            chip_z = meta["chiplet_pos"][2]
        place.add(Chiplet(
            f"chiplet{chiplet_id}", (chip_x, chip_y, chip_z),
            example_chip["width"] * shape[0] / k_node,
            example_chip["height"] * shape[1] / k_node,
            example_place["chiplet_keepout"], example_place["chiplet_keepout"],
            1, example_chip["width"], example_chip["height"],
        ))
    threshold = _FULL_DISTANCES_MAX_CHIPLETS if full_distances_max_chiplets is None else int(full_distances_max_chiplets)
    if threshold < 0:
        raise ValueError("full_distances_max_chiplets must be non-negative")
    if layers > 1:
        place.rowscols(grid_data["grid_shape"][1], grid_data["grid_shape"][0])
    elif chiplets > threshold:
        place.finalize(collinear_only=True)
    else:
        place.finalize()
    # The runtime only consumes two placement-derived technology scalars.  Do
    # not serialize the sparse pair table: even the collinear-only table grows
    # to hundreds of thousands of YAML mappings for large models.
    facing = {}
    stack_areas = []
    for (a, b), metrics in place.distances.items():
        overlap = float(metrics["side_overlap_m"])
        if overlap > 0.0:
            facing[a] = facing.get(a, 0.0) + overlap
            facing[b] = facing.get(b, 0.0) + overlap
        area = float(metrics["stack_overlap_m2"])
        if area > 0.0:
            stack_areas.append(area)
    placement = place._asdict()
    placement["distances"] = {}
    placement["derived_chiplet_beachfront_um"] = min(facing.values()) * 1.0e3 if facing else 0.0
    placement["derived_stack_overlap_um2"] = min(stack_areas) * 1.0e6 if stack_areas else 0.0
    if layers > 1:
        # Shared conservative scalar; individual vertical links use pair dimensions.
        chips = list(place.chiplets.values())
        w, h, _ = grid_data["grid_shape"]
        placement["derived_chiplet_beachfront_um"] = ((w-1)*min(c.height for c in chips) + (h-1)*min(c.width for c in chips))*1e3
        plane = w*h
        placement["derived_stack_overlap_um2"] = min(min(chips[i].width,chips[i-plane].width)*min(chips[i].height,chips[i-plane].height) for i in range(plane,len(chips)))*1e6
    configs["placement"] = placement
    outputs = {}
    for name, data in configs.items():
        path = outdir / f"{name}.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        outputs[name] = path
    if grid_data is not None and nonuniform_grid_file is not None:
        grid_output = outdir / "nonuniform_grid.json"
        grid_output.write_text(json.dumps(grid_data, indent=2), encoding="utf-8")
        outputs["nonuniform_grid"] = grid_output
    provenance = {
        "geometry": {"k_chip": k_chip, "k_node": k_node, "chiplets": chiplets, "routers": routers},
        "runtime_physics": {
            "material": material,
            "bonding": bonding,
            "network_options": tuning,
            "chiplet_clock": configs["system"]["chiplet_clock"],
            "noi_clock": configs["system"]["noi_clock"],
        "onchip_width_bits": net["onchip_width"],
        "offchip_width_bits": net.get("offchip_width"),
        "vc_number": net["top_vc"],
            "flit_bits": net["flit_bits"],
            "flit_bits_role": "bandwidth_quantization",
            "flit_width_mode": "native_port_width",
        },
        "parameter_sources": {
            name: {"path": str(path.relative_to(_EXAMPLES)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in {
                **sources,
                "placement": _EXAMPLES / "placement/place_model_2_5d.yaml",
            }.items()
        },
    }
    (outdir / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    return outputs
