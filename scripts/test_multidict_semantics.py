"""Compare mutable inherited parameter tables against the pre-optimization source."""
import random
import runpy
import subprocess
from pathlib import Path

GEM5 = Path(__file__).resolve().parents[1] / 'gem5'
SOURCE = 'src/python/m5/util/multidict.py'
reference = {'__name__': 'reference_multidict'}
exec(subprocess.check_output(
    ['git', '-C', str(GEM5), 'show', f'367f6bf:{SOURCE}'], text=True
), reference)
old_type = reference['multidict']
new_type = runpy.run_path(str(GEM5 / SOURCE))['multidict']
old, new = [old_type()], [new_type()]
for _ in range(7):
    old.append(old_type(old[-1]))
    new.append(new_type(new[-1]))
rng = random.Random(7)
for step in range(2000):
    level, key = rng.randrange(8), str(rng.randrange(24))
    if rng.randrange(3):
        old[level][key] = new[level][key] = step
    else:
        missing = []
        for chain in (old, new):
            try:
                del chain[level][key]
                missing.append(False)
            except KeyError:
                missing.append(True)
        assert missing[0] == missing[1], (step, level, key)
    for before, after in zip(old, new):
        assert list(before.items()) == list(after.items()), step
        for k in map(str, range(24)):
            # Preserve gem5's existing membership semantics for tombstones.
            assert (k in before) == (k in after), (step, k)
            assert before.get(k, None) == after.get(k, None), (step, k)
print('multidict semantics OK: 2000 mutations across 8 inheritance levels')
