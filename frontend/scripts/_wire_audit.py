"""Evidence pass: which API functions are actually referenced from views?

Run from `frontend/`. Prints a per-module wired/unwired table so the handoff doc can state the
unwired-endpoint column from measured fact rather than memory.
"""

import io
import os
import re

API_DIR = os.path.join('src', 'api')
VIEW_DIRS = [os.path.join('src', 'views'), os.path.join('src', 'components'), os.path.join('src', 'stores')]

# Collect api function names per module.
MODULES = {}
for name in sorted(os.listdir(API_DIR)):
    if not name.endswith('.ts') or name in ('index.ts', 'client.ts', 'error.ts', 'tokenStore.ts'):
        continue
    path = os.path.join(API_DIR, name)
    with io.open(path, encoding='utf-8') as handle:
        text = handle.read()
    names = set(re.findall(r'^\s{2}async (\w+)\(', text, re.M))
    if names:
        MODULES[name] = names

# Collect every identifier referenced anywhere under src/ EXCEPT the api layer itself,
# so we measure "used by a view/store/component", not "used by another api function".
USE_TEXT = []
for root in VIEW_DIRS:
    for dirpath, _dirs, files in os.walk(root):
        for filename in files:
            if filename.endswith(('.ts', '.vue')):
                with io.open(os.path.join(dirpath, filename), encoding='utf-8', errors='replace') as handle:
                    USE_TEXT.append(handle.read())
USES = '\n'.join(USE_TEXT)

print(f'{"module":<16} {"function":<24} wired_from_view')
print('-' * 60)
unwired = []
for module, names in MODULES.items():
    for fn in sorted(names):
        wired = bool(re.search(r'\b' + re.escape(fn) + r'\s*\(', USES))
        if not wired:
            unwired.append(f'{module}.{fn}')
        print(f'{module:<16} {fn:<24} {"YES" if wired else "NO"}')

print()
print(f'UNWIRED ({len(unwired)}):')
for item in unwired:
    print('  -', item)
