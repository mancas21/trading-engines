import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, 'r', encoding='utf-8') as fh:
    data = json.load(fh)

for i, w in enumerate(data.get('Windows', [])):
    for j, tab in enumerate(w.get('Tabs', [])):
        print(f"=== Tab {j} keys: {list(tab.keys())} ===")
        for k, v in tab.items():
            if k == 'Indicators':
                print(f"  Indicators ({len(v)}):")
                for ind in v:
                    print(f"    Name: {ind.get('Name', '?')}")
                    print(f"    Type: {ind.get('Type', '?')}")
                    print(f"    Enabled: {ind.get('Enabled', '?')}")
                    print(f"    ---")
            elif isinstance(v, (list, dict)):
                print(f"  {k}: {type(v).__name__} len={len(v)}")
            else:
                print(f"  {k}: {v}")
        print()
