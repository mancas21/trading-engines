import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

# Remove ALL LiveExporter entries from everywhere
def clean_indicators(obj):
    if isinstance(obj, dict):
        items_to_process = []
        for k, v in obj.items():
            if k in ('SerializedIndicators', 'Indicators') and isinstance(v, list):
                # Filter out LiveExporter
                obj[k] = [x for x in v if 'LiveExporter' not in x]
            elif isinstance(v, (dict, list)):
                items_to_process.append(v)
        for item in items_to_process:
            clean_indicators(item)
    elif isinstance(obj, list):
        for item in obj:
            clean_indicators(item)

clean_indicators(d)

with open(f, 'w', encoding='utf-8') as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)

# Verify
verify_panels = []
def find_panels(node):
    if isinstance(node, dict):
        if 'SerializedIndicators' in node:
            verify_panels.append(node)
        for v in node.values():
            find_panels(v)
    elif isinstance(node, list):
        for item in node:
            find_panels(item)

find_panels(d)
for p in verify_panels:
    names = []
    for si in p['SerializedIndicators']:
        parsed = json.loads(si)
        name = parsed['Type'].split(',')[0].split('.')[-1]
        names.append(name)
    print(f"Panel: {names}")

def find_template(node):
    if isinstance(node, dict):
        if 'Template' in node:
            ti = node['Template'].get('Indicators', [])
            names = []
            for si in ti:
                parsed = json.loads(si)
                name = parsed['Type'].split(',')[0].split('.')[-1]
                names.append(name)
            print(f"Template Indicators: {names}")
        for v in node.values():
            find_template(v)
    elif isinstance(node, list):
        for item in node:
            find_template(item)

find_template(d)
print("✅ Workspace cleaned")
