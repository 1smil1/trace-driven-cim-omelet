from m5.params import *
from m5.objects import *
from ..builders import *
import math
from collections import defaultdict

def build_vertical_inter_chiplet(opts, Techlib, top_noc_cfg, bottom_noc_cfg, nol_cfg, sys_cfg, plc_cfg, network,
                    routers, IntLink, top_routerbase, bottom_routerbase, link_base, top_chiplet, bot_chiplet):

    int_links = []
    link_cnt = 0
    from omelet.nox.vertical import resolve_vertical_method, vertical_ports, vertical_port_width
    top_rows, bot_rows = top_noc_cfg['mesh_rows'], bottom_noc_cfg['mesh_rows']
    top_count, bot_count = sys_cfg['noc_routers_per_chiplet'][top_chiplet], sys_cfg['noc_routers_per_chiplet'][bot_chiplet]
    if top_count % top_rows or bot_count % bot_rows:
        raise ValueError('NoC router count must divide mesh_rows')
    top_grid, bot_grid = [top_count//top_rows,top_rows], [bot_count//bot_rows,bot_rows]
    requested = sys_cfg.get('vertical_interconnect_method',0) if opts.vertical_interconnect_method is None else opts.vertical_interconnect_method
    pair_grid = {'grid_shape':[1,1,2], 'chiplets':{'0':{'grid':bot_grid},'1':{'grid':top_grid}}}
    method = resolve_vertical_method(pair_grid, requested)
    ports = vertical_ports(bot_grid,top_grid,method)
    width = vertical_port_width(sys_cfg,plc_cfg,bot_chiplet,top_chiplet,len(ports),opts.c2c_bonding) if method == 1 else None

    for bot_local, top_local in ports:
        noi_clk = SrcClockDomain(clock = sys_cfg["chiplet_clock"],
                                voltage_domain = VoltageDomain(voltage = sys_cfg["sys_voltage"]))
        l_down = make_link(opts=opts, tech=Techlib, sys_cfg=sys_cfg, plc_cfg=plc_cfg, nox_cfg=top_noc_cfg,
                    IntLinks=IntLink,
                    lid=link_base + link_cnt,
                    src_router=routers[top_routerbase + top_local],
                    dst_router=routers[bottom_routerbase + bot_local],
                    src_outport = "Down",
                    dst_inport  = "Up",
                    weight=1,
                    length=None, via_width=width)
        int_links.append(l_down)
        link_cnt += 1

        noi_clk = SrcClockDomain(clock = sys_cfg["chiplet_clock"],
                                voltage_domain = VoltageDomain(voltage = sys_cfg["sys_voltage"]))
        l_up = make_link(opts=opts, tech=Techlib, sys_cfg=sys_cfg, plc_cfg=plc_cfg, nox_cfg=top_noc_cfg,
                    IntLinks=IntLink,
                    lid=link_base + link_cnt,
                    src_router=routers[bottom_routerbase + bot_local],
                    dst_router=routers[top_routerbase + top_local],
                    src_outport = "Up",
                    dst_inport  = "Down",
                    weight=1,
                    length=None, via_width=width)
        int_links.append(l_up)
        link_cnt += 1

    return int_links, link_cnt
