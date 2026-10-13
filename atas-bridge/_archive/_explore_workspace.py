import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, 'r', encoding='utf-8') as fh:
    data = json.load(fh)

print("Top keys:", list(data.keys()))
print("Windows count:", len(data.get('Windows', [])))
for i, w in enumerate(data.get('Windows', [])):
    print(f"\n=== Window {i} ===")
    for k, v in w.items():
        if k == 'Tabs':
            print(f"  Tabs: {len(v)} items")
            for j, tab in enumerate(v):
                print(f"  --- Tab {j}: {tab.get('Name', '?')} ---")
                for tk, tv in tab.items():
                    if tk == 'Indicators':
                        print(f"    Indicators: {len(tv)} items")
                        for ind in tv:
                            print(f"      - {ind.get('Name','?')} | Type: {ind.get('Type','?')} | Enabled: {ind.get('Enabled','?')}")
                    elif tk in ('Name', 'Symbol', 'TimeFrame', 'ChartType'):
                        print(f"    {tk}: {tv}")
                    elif tk in ('IndicatorsLayout', 'ChartLayout'):
                        print(f"    {tk}: {list(tv.keys()) if isinstance(tv, dict) else tv}")
        else:
            print(f"  {k}: {v}")
