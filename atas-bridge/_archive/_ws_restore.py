import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

# Remove LiveExporter from ALL SerializedIndicators AND Template.Indicators
def remove_from_all(node):
    if isinstance(node, dict):
        if 'SerializedIndicators' in node and isinstance(node['SerializedIndicators'], list):
            node['SerializedIndicators'] = [si for si in node['SerializedIndicators'] if 'LiveExporter' not in si]
        if 'Indicators' in node and isinstance(node['Indicators'], list):
            node['Indicators'] = [si for si in node['Indicators'] if 'LiveExporter' not in si]
        for v in node.values():
            remove_from_all(v)
    elif isinstance(node, list):
        for item in node:
            remove_from_all(item)

remove_from_all(d)

# Add to Panel 0 SerializedIndicators
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
le = {
    "Type": "AtasExporter.LiveExporter, AtasExporter, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
    "Settings": json.dumps({"Name": "LiveExporter"})
}
panels[0]['SerializedIndicators'].append(json.dumps(le))
print(f"Panel 0 now has {len(panels[0]['SerializedIndicators'])} indicators")

with open(f, 'w', encoding='utf-8') as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)
print("Saved")
