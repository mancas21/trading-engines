import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

# Remove LiveExporter from ALL SerializedIndicators
def remove_from_all_panels(node):
    if isinstance(node, dict):
        if 'SerializedIndicators' in node and isinstance(node['SerializedIndicators'], list):
            node['SerializedIndicators'] = [
                si for si in node['SerializedIndicators'] if 'LiveExporter' not in si
            ]
        for v in node.values():
            remove_from_all_panels(v)
    elif isinstance(node, list):
        for item in node:
            remove_from_all_panels(item)

remove_from_all_panels(d)

# Add to Template.Indicators
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
    le_entry = {
        "Type": "AtasExporter.LiveExporter, AtasExporter, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Settings": json.dumps({
            "Name": "LiveExporter"
        })
    }
    t['Indicators'].append(json.dumps(le_entry))
    print(f"Template.Indicators now has {len(t['Indicators'])} items")

with open(f, 'w', encoding='utf-8') as fh:
    json.dump(d, fh, indent=2, ensure_ascii=False)
print("Done")
