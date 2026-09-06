import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, 'r', encoding='utf-8') as fh:
    data = json.load(fh)

for j, tab in enumerate(data['Windows'][0]['Tabs']):
    print(f"=== Tab {j}: {tab.get('Title','?')} ===")
    # Check if this tab has a chart with indicators
    layout = tab.get('Layout', {})
    items = [layout]
    while items:
        item = items.pop(0)
        t = item.get('Type', '')
        p = item.get('Parameter', {})
        if isinstance(p, dict):
            items.append(p)
        if 'Indicators' in item:
            print(f"  Indicators found!")
            for ind in item['Indicators']:
                print(f"    {ind.get('Name','?')} | {ind.get('Type','?')}")
        if isinstance(p, dict) and 'Indicators' in p:
            print(f"  Indicators in param!")
            for ind in p['Indicators']:
                print(f"    {ind.get('Name','?')} | {ind.get('Type','?')}")
