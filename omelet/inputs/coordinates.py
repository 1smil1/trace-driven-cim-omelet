"""Explicit placement formats shared by the standalone and chip backends."""
import warnings
from pathlib import Path
from numbers import Integral


def physical_position(value):
    if len(value) == 4:
        value = (*value[:2], 0, *value[2:])
    if len(value) != 5 or any(isinstance(v, bool) or not isinstance(v, Integral) or v < 0 for v in value):
        raise ValueError(f"Expected nonnegative (chip_x,chip_y,z,row,col): {value}")
    return tuple(int(v) for v in value)


def xyz(value):
    if len(value) == 2:
        value = (*value, 0)
    if len(value) != 3 or any(type(v) is not int or v < 0 for v in value):
        raise ValueError(f"Expected nonnegative (x,y,z): {value}")
    return tuple(value)


def position_rows(path):
    legacy = False
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            fields = line.split()
            if len(fields) == 7:
                fields.insert(3, "0")
                legacy = True
            if len(fields) != 8:
                raise ValueError(f"{path}:{number}: expected 8 position columns")
            pid, x, y, z, tier, chip = map(int, fields[:6])
            kind, cnode = fields[6], int(fields[7])
            if min(pid, x, y, z, cnode) < 0 or kind not in ("real", "virtual"):
                raise ValueError(f"{path}:{number}: invalid position row")
            yield pid, x, y, z, tier, chip, kind, cnode
    if legacy:
        warnings.warn(f"LEGACY POSITION ADAPTER: {path}; seven columns mean z=0; re-export eight columns", UserWarning)


def normalize_grid(data):
    if data["enabled"] is not True:
        raise ValueError("Routing grid must be enabled")
    shape = xyz(data["grid_shape"])
    # Legacy grid_shape is dimensions, so its third dimension is one.
    if len(data["grid_shape"]) == 2:
        shape = (*shape[:2], 1)
    if min(shape) <= 0:
        raise ValueError("Routing grid dimensions must be positive")
    data["grid_shape"] = list(shape)
    occupied, base = set(), 0
    for index in range(shape[0] * shape[1] * shape[2]):
        meta = data["chiplets"][str(index)]
        pos, origin = xyz(meta["chiplet_pos"]), xyz(meta["origin"])
        width, height = meta["grid"]
        if any(type(v) is not int or v <= 0 for v in (width, height)):
            raise ValueError(f"Chiplet {index}: invalid local grid")
        if pos != (index % shape[0], index // shape[0] % shape[1], index // (shape[0] * shape[1])):
            raise ValueError(f"Chiplet {index}: inconsistent layer-major slot")
        if origin[2] != pos[2] or meta["node_id_base"] != base:
            raise ValueError(f"Chiplet {index}: inconsistent origin/base")
        meta["chiplet_pos"], meta["origin"] = list(pos), list(origin)
        occupied.add(pos)
        base += width * height
    if len(data["chiplets"]) != len(occupied):
        raise ValueError("Grid must contain exactly all occupied and transit slots")
    if shape[2] > 1:
        plane = shape[0] * shape[1]
        for index in range(plane, len(data["chiplets"])):
            if data["chiplets"][str(index)]["origin"][:2] != data["chiplets"][str(index % plane)]["origin"][:2]:
                raise ValueError(f"Chiplet {index}: 3D layers must have aligned origins")
    for link in data["links"]:
        a = data["chiplets"][str(link["src_chiplet"])]
        b = data["chiplets"][str(link["dst_chiplet"])]
        if a["chiplet_pos"][2] or b["chiplet_pos"][2]:
            raise ValueError("Only bottom-layer chiplets may attach to NoI")
    return data


def stack_supported_slots(slots, plane):
    occupied = set(slots)
    return bool(occupied) and all(p < plane or p-plane in occupied for p in occupied)


def settle_stack_slots(slots, plane):
    settled = list(slots)
    columns = {}
    for i, p in enumerate(slots):
        columns.setdefault(p % plane, []).append((p, i))
    for column, members in columns.items():
        for layer, (_, i) in enumerate(sorted(members)):
            settled[i] = column + layer * plane
    return settled


def reject_3d(position_path, grid=None):
    for pid, x, y, z, tier, chip, kind, cnode in position_rows(position_path):
        if z:
            raise ValueError(f"CNSim does not support 3D: {position_path}, node={pid}, chiplet={chip}, z={z}")
    if grid is not None:
        normalize_grid(grid)
        if grid["grid_shape"][2] != 1:
            raise ValueError("CNSim does not support 3D: routing grid has multiple layers")
