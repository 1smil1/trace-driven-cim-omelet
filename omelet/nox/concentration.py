"""Mesh regions attached to NoI routers, without gem5 objects."""
import math


def concentrated_regions(grid, concentration):
    side = math.isqrt(concentration)
    if side * side != concentration or side == 0:
        raise ValueError('NoI concentration must be a positive square')
    regions = {}
    count = 0
    for chip_id in sorted(grid['chiplets'], key=int):
        width, height = grid['chiplets'][chip_id]['grid']
        columns, rows = (width + side - 1)//side, (height + side - 1)//side
        regions[chip_id] = {'base': count, 'cols': columns, 'rows': rows, 'side': side}
        count += columns * rows
    return regions, count
