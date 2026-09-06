import json, os

f = os.path.join(os.environ['APPDATA'], 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')
with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

def find_template(node, depth=0):
    if depth > 20:
        return None
    if isinstance(node, dict):
        if 'Template' in node:
            return node['Template']
        for v in node.values():
            if isinstance(v, (dict, list)):
                r = find_template(v, depth+1)
                if r:
                    return r
    elif isinstance(node, list):
        for item in node:
            r = find_template(item, depth+1)
            if r:
                return r
    return None

t = find_template(d)
print('Template Indicators:', t.get('Indicators', []))

def find_panels(node, depth=0):
    results = []
    if depth > 20:
        return results
    if isinstance(node, dict) and 'SerializedIndicators' in node:
        results.append(node)
    for v in (node.values() if isinstance(node, dict) else node):
        if isinstance(v, (dict, list)):
            results.extend(find_panels(v, depth+1))
    return results

panels = find_panels(d)
for i, p in enumerate(panels):
    sis = p['SerializedIndicators']
    print(f'Panel {i}: {len(sis)} indicators')
    for si in sis:
        parsed = json.loads(si)
        tname = parsed['Type'].split(',')[0].split('.')[-1]
        print(f'  - {tname}')
