import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, 'r', encoding='utf-8') as fh:
    data = json.load(fh)

def find_all_panels(node, depth=0):
    results = []
    if depth > 20:
        return results
    if isinstance(node, dict):
        if 'SerializedIndicators' in node:
            results.append(node)
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                results.extend(find_all_panels(v, depth+1))
    elif isinstance(node, list):
        for item in node:
            results.extend(find_all_panels(item, depth+1))
    return results

panels = find_all_panels(data)
print(f"Found {len(panels)} panels with SerializedIndicators:")
for i, panel in enumerate(panels):
    print(f"\nPanel {i}:")
    print(f"  Keys: {list(panel.keys())}")
    print(f"  Indicators: {len(panel['SerializedIndicators'])}")
    for j, si in enumerate(panel['SerializedIndicators']):
        try:
            parsed = json.loads(si)
            t = parsed.get('Type', '')
            name = t.split(',')[0].split('.')[-1]
            print(f"    [{j}] {name}")
        except Exception as e:
            print(f"    [{j}] Error: {e}")
