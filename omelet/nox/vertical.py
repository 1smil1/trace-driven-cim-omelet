"""Deterministic vertical port mapping between independent NoC meshes."""
import warnings


def resolve_vertical_method(grid, requested=0):
    if type(requested) is not int or requested not in (0, 1):
        raise ValueError('vertical_interconnect_method must be 0 or 1')
    if grid is None or grid['grid_shape'][2] == 1:
        return requested
    plane = grid['grid_shape'][0]*grid['grid_shape'][1]
    unequal = any(grid['chiplets'][str(i)]['grid'] != grid['chiplets'][str(i-plane)]['grid']
                  for i in range(plane, len(grid['chiplets'])))
    if requested == 0 and unequal:
        warnings.warn('OMELET: heterogeneous 3D NoC grids detected; automatically converting vertical-interconnect-method 0 -> 1', UserWarning)
        return 1
    return requested


def vertical_ports(lower, upper, method):
    if method == 0:
        if lower != upper:
            raise ValueError('Native vertical links require matching NoC grids')
        return [(i,i) for i in range(lower[0]*lower[1])]
    if method != 1:
        raise ValueError('vertical_interconnect_method must be 0 or 1')
    nx, ny = min(lower[0], upper[0]), min(lower[1], upper[1])
    def index(k, size, count):
        return (size-1)//2 if count == 1 else (k*(size-1)+(count-1)//2)//(count-1)
    return [(index(y,lower[1],ny)*lower[0]+index(x,lower[0],nx),
             index(y,upper[1],ny)*upper[0]+index(x,upper[0],nx))
            for y in range(ny) for x in range(nx)]


def vertical_port_width(system, placement, lower, upper, ports, bonding):
    from omelet.linkmodel.width import vertical_width_bits, BOND_PITCH_UM
    a, b = placement['chiplets'][lower], placement['chiplets'][upper]
    overlap_um2 = min(a['width'],b['width'])*min(a['height'],b['height'])*1e6
    area = min(system['via_signal_area_mm2']*1e6, overlap_um2)
    width = vertical_width_bits(area/ports, BOND_PITCH_UM[bonding], system['flit_bits'])
    if width <= 0:
        raise ValueError(f'Vertical signal area cannot supply one width quantum per port: {lower}<->{upper}, ports={ports}')
    return width
