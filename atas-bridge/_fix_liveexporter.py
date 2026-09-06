import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

# Remove LiveExporter from panel SerializedIndicators
def remove_from_panels(node, depth=0):
    if depth > 20:
        return
    if isinstance(node, dict):
        if 'SerializedIndicators' in node and isinstance(node['SerializedIndicators'], list):
            removed = 0
            node['SerializedIndicators'] = [
                si for si in node['SerializedIndicators']
                if 'LiveExporter' not in si
            ]
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                remove_from_panels(v, depth+1)
    elif isinstance(node, list):
        for item in node:
            remove_from_panels(item, depth+1)

remove_from_panels(d)

with open(f, 'w', encoding='utf-8') as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)

print("✅ Removed LiveExporter from panels, kept in Template.Indicators")
print("Restart ATAS X needed")
