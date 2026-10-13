import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

# Print the full panel structure for Tab 2
tab2 = d['Windows'][0]['Tabs'][2]
layout = tab2['Layout']
print("Full path to panels:")
# Navigate to find the panels with path
def find_panels(node, path=''):
    if isinstance(node, dict):
        if 'Panels' in node and isinstance(node['Panels'], list):
            print(f"\n{path}.Panels:")
            for i, panel in enumerate(node['Panels']):
                print(f"  Panel {i}: keys={list(panel.keys())}")
                if 'SerializedIndicators' in panel:
                    for j, si in enumerate(panel['SerializedIndicators']):
                        parsed = json.loads(si)
                        t = parsed.get('Type', '')
                        name = t.split(',')[0].split('.')[-1]
                        sets = json.loads(parsed.get('Settings', '{}'))
                        print(f"    [{j}] {name} | Enabled={sets.get('Enabled','?')}")
                if 'Height' in panel:
                    print(f"    Height: {panel.get('Height')}")
                if 'Collapsed' in panel:
                    print(f"    Collapsed: {panel.get('Collapsed')}")
                if 'IsNew' in panel:
                    print(f"    IsNew: {panel.get('IsNew')}")
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                find_panels(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            find_panels(item, f"{path}[{i}]")

find_panels(d)
