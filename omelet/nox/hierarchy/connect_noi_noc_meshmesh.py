from m5.params import *
from m5.objects import *
import math
from collections import defaultdict
from ..builders import *
from ..concentration import concentrated_regions

def calc_noc_noi_map(
        chiplet_rows, chiplet_cols,
        noc_rows_pc, noc_cols_pc,
        conc_factor,              
        noi_rows, noi_cols,
        base_noc_id=0, base_noi_id=0):

    tot_noc_rows = chiplet_rows * noc_rows_pc 
    tot_noc_cols = chiplet_cols  * noc_cols_pc

    mapping = defaultdict(list)

    def noc_id(g_row, g_col):
        cr, lr = divmod(g_row, noc_rows_pc)
        cc, lc = divmod(g_col, noc_cols_pc)
        chiplet_id  = cr * chiplet_cols + cc
        local_id    = lr * noc_cols_pc + lc
        return base_noc_id + chiplet_id * (noc_rows_pc * noc_cols_pc) + local_id

    for nr in range(noi_rows):           
        row_band = range(nr * conc_factor, min((nr + 1) * conc_factor, tot_noc_rows))
        for nc in range(noi_cols):    
            col_band = range(nc * conc_factor, min((nc + 1) * conc_factor, tot_noc_cols))
            noi_id = base_noi_id + nr * noi_cols + nc
            for r in row_band:
                for c in col_band:
                    if r < tot_noc_rows and c < tot_noc_cols:
                        mapping[noi_id].append(noc_id(r, c))
    return mapping

def connect_noi_noc(opts, Techlib, noc_cfg, noi_cfg, sys_cfg, plc_cfg, network, routers, IntLink, nodes, link_base, noc_routers_per_chiplet_list, router_map, nonuniform_grid=None, progress=None):
    if nonuniform_grid is not None:
        from omelet.inputs.coordinates import normalize_grid
        normalize_grid(nonuniform_grid)
        chiplets = {k: v for k, v in nonuniform_grid["chiplets"].items() if v["chiplet_pos"][2] == 0}
        bottom_grid = {**nonuniform_grid, "chiplets": chiplets}
        links = nonuniform_grid["links"]
        noi_base = router_map["interposer"][0]
        int_links = []
        link_count = 0
        attached = set()
        noi_pairs = set()
        concentrated = noi_cfg.get("concentrated_mapping", False)
        if concentrated:
            regions, count = concentrated_regions(bottom_grid, noi_cfg["router"]["concentration"])
            if count != noi_cfg["num_routers"]:
                raise ValueError("NoI region count does not match router configuration")

        def local_noi(chip, x, y):
            if not concentrated:
                return noi_base + chip
            region = regions[str(chip)]
            return noi_base + region["base"] + (y // region["side"]) * region["cols"] + x // region["side"]

        def add_link(src_id, dst_id, src_outport, dst_inport, cfg):
            nonlocal link_count
            # Match the native mesh's X/Y weights; keep NoC-NoI attachments at 1.
            weight = 2 if src_outport in ("North", "South") else 1
            int_links.append(make_link(opts=opts, tech=Techlib, sys_cfg=sys_cfg, plc_cfg=plc_cfg, nox_cfg=cfg, IntLinks=IntLink, lid=link_base + link_count, src_router=routers[src_id], dst_router=routers[dst_id], src_outport=src_outport, dst_inport=dst_inport, weight=weight, length=None))
            link_count += 1

        if progress is not None:
            progress(f"creating NoC-NoI boundary links 0/{len(links)} boundary_entries")
        for completed, row in enumerate(links, 1):
            src_chip = int(row["src_chiplet"])
            dst_chip = int(row["dst_chiplet"])
            src_shape = chiplets[str(src_chip)]["grid"]
            dst_shape = chiplets[str(dst_chip)]["grid"]
            src_base = router_map[f"chiplet{src_chip}"][0]
            dst_base = router_map[f"chiplet{dst_chip}"][0]
            sx, sy = (int(row["src_local"][0]), int(row["src_local"][1]))
            dx, dy = (int(row["dst_local"][0]), int(row["dst_local"][1]))
            src_router = src_base + sy * int(src_shape[0]) + sx
            dst_router = dst_base + dy * int(dst_shape[0]) + dx
            src_noi = local_noi(src_chip, sx, sy)
            dst_noi = local_noi(dst_chip, dx, dy)
            if (src_chip, src_router) not in attached:
                add_link(src_router, src_noi, "Down", f"Up{src_router}", noi_cfg)
                add_link(src_noi, src_router, f"Up{src_router}", "Down", noi_cfg)
                attached.add((src_chip, src_router))
            if (dst_chip, dst_router) not in attached:
                add_link(dst_router, dst_noi, "Down", f"Up{dst_router}", noi_cfg)
                add_link(dst_noi, dst_router, f"Up{dst_router}", "Down", noi_cfg)
                attached.add((dst_chip, dst_router))
            pair = tuple(sorted((src_noi, dst_noi)))
            if pair not in noi_pairs:
                forward, reverse = ("East", "West") if row["direction"] == "east" else ("North", "South")
                add_link(src_noi, dst_noi, forward, reverse, noi_cfg)
                add_link(dst_noi, src_noi, reverse, forward, noi_cfg)
                noi_pairs.add(pair)
            if progress is not None and (completed % 100 == 0 or completed == len(links)):
                progress(f"creating NoC-NoI boundary links {completed}/{len(links)} boundary_entries")
        if concentrated:
            # Each block remains a physical mesh region, including empty transit cells.
            for chip, region in regions.items():
                width, height = chiplets[chip]["grid"]
                base = router_map[f"chiplet{chip}"][0]
                for y in range(height):
                    for x in range(width):
                        router = base + y * width + x
                        noi = local_noi(int(chip), x, y)
                        if (int(chip), router) not in attached:
                            add_link(router, noi, "Down", f"Up{router}", noi_cfg)
                            add_link(noi, router, f"Up{router}", "Down", noi_cfg)
                            attached.add((int(chip), router))
                for y in range(region["rows"]):
                    for x in range(region["cols"]):
                        src = noi_base + region["base"] + y * region["cols"] + x
                        for nx, ny, forward, reverse in ((x+1, y, "East", "West"), (x, y+1, "North", "South")):
                            if nx < region["cols"] and ny < region["rows"]:
                                dst = noi_base + region["base"] + ny * region["cols"] + nx
                                add_link(src, dst, forward, reverse, noi_cfg)
                                add_link(dst, src, reverse, forward, noi_cfg)
        return int_links, link_count

    int_links = []
    link_cnt = 0

    chiplet_rows = plc_cfg["rows"]
    chiplet_cols = plc_cfg["cols"]
    conc = int(math.sqrt(noi_cfg["router"]["concentration"])) 

    noc_max_rows, noc_max_cols = 0, 0
    for i in range(Techlib.num_chiplets):
        num_routers = noc_routers_per_chiplet_list[f"chiplet{i}"]
        rows = noc_cfg[i]["mesh_rows"]
        cols = num_routers // rows
        if (rows > noc_max_rows): noc_max_rows = rows
        if (cols > noc_max_cols): noc_max_cols = cols

    noc_rows_total = noc_max_rows * chiplet_rows
    noc_cols_total = noc_max_cols * chiplet_cols

    noi_rows = noi_cfg["mesh_rows"]
    noi_cols = noi_cfg["num_routers"] // noi_cfg["mesh_rows"]

    mapping = calc_noc_noi_map(
        chiplet_rows=chiplet_rows,
        chiplet_cols=chiplet_cols,
        noc_rows_pc=noc_max_rows,
        noc_cols_pc=noc_max_cols,
        conc_factor=conc, 
        noi_rows=noi_rows,
        noi_cols=noi_cols,
        base_noc_id=0,
        base_noi_id=router_map["interposer"][0]
    )

    if progress is not None:
        progress(f"creating NoC-NoI boundary links 0/{len(mapping)} NoI_routers")
    for completed, (noi_id, noc_list) in enumerate(mapping.items(), 1):
        for noc_id in noc_list:
            need_serdes = (sys_cfg["noc_bitwidth"] != sys_cfg["noi_bitwidth"])

            noi_clk = SrcClockDomain(clock = sys_cfg["noi_clock"],
                                    voltage_domain = VoltageDomain(voltage = sys_cfg["sys_voltage"]))

            l_down = make_link(opts=opts, tech=Techlib, sys_cfg=sys_cfg, plc_cfg=plc_cfg, nox_cfg=noi_cfg,
                        IntLinks=IntLink,
                        lid=link_base + link_cnt,
                        src_router=routers[noc_id],
                        dst_router=routers[noi_id],
                        src_outport = "Down",
                        dst_inport  = "Up",
                        weight=1, 
                        length=None)
            int_links.append(l_down)
            
            
            link_cnt += 1
            noi_clk = SrcClockDomain(clock = sys_cfg["noi_clock"],
                                    voltage_domain = VoltageDomain(voltage = sys_cfg["sys_voltage"]))

            l_up = make_link(opts=opts, tech=Techlib, sys_cfg=sys_cfg, plc_cfg=plc_cfg, nox_cfg=noi_cfg,
                        IntLinks=IntLink,
                        lid=link_base + link_cnt,
                        src_router=routers[noi_id],
                        dst_router=routers[noc_id],
                        src_outport = "Up",
                        dst_inport  = "Down",
                        weight=1,
                        length=None)
            int_links.append(l_up)

            link_cnt += 1

        if progress is not None and (completed % 100 == 0 or completed == len(mapping)):
            progress(f"creating NoC-NoI boundary links {completed}/{len(mapping)} NoI_routers")

    return int_links, link_cnt
