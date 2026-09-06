import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

tab2 = d['Windows'][0]['Tabs'][2]
panels = tab2['Layout']['Parameter']['Item1']['Parameter']['Template']['Panels']

# Show full serialized indicators for panel 0
print("=== Panel 0 - DOM ===")
si0 = json.loads(panels[0]['SerializedIndicators'][0])
print("Type:", si0['Type'])
print("Settings:", si0['Settings'])

print("\n=== Panel 0 - LiveExporter (ours) ===")
si1 = json.loads(panels[0]['SerializedIndicators'][1])
print("Type:", si1['Type'])
print("Settings:", si1['Settings'])
