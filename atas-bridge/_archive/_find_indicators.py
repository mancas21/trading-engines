import json, os, re

f = os.path.join(os.environ['APPDATA'], 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')
with open(f, encoding='utf-8') as fh:
    data = json.load(fh)

s = json.dumps(data)

# find all 'Indicator' mentions
for m in re.finditer(r'"Indicator"\s*:\s*\{', s):
    start = m.start()
    snippet = s[start:start+400]
    print(snippet)
    print('---')

# Also search for any Names that look like indicator names
for m in re.finditer(r'"Name"\s*:\s*"([^"]+)"', s):
    name = m.group(1)
    if name not in ('SOLUSDT SMART TAPE', 'SOLUSDT', 'Tab') and len(name) > 2:
        # Check context - what's before this?
        ctx_start = max(0, m.start()-200)
        ctx = s[ctx_start:m.start()]
        if 'Indicator' in ctx or 'indicator' in ctx.lower():
            print(f"Indicator name: {name}")
