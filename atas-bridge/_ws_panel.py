import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

# Remove LiveExporter from Template.Indicators
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
if t and 'Indicators' in t:
    t['Indicators'] = [si for si in t['Indicators'] if 'LiveExporter' not in si]
    print("Removed from Template.Indicators")

# Add to panel 0's SerializedIndicators
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
le_entry = {
    "Type": "AtasExporter.LiveExporter, AtasExporter, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
    "Settings": json.dumps({
        "Name": "LiveExporter"
    })
}
panels[0]['SerializedIndicators'].append(json.dumps(le_entry))
print(f"Added to Panel 0 (now {len(panels[0]['SerializedIndicators'])} indicators)")

with open(f, 'w', encoding='utf-8') as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)
print("Workspace saved")
