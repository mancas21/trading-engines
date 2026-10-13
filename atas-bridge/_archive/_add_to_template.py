import json, os

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, encoding='utf-8') as fh:
    d = json.load(fh)

tab2 = d['Windows'][0]['Tabs'][2]
layout = tab2['Layout']

# Navigate to Template
def find_template(node, depth=0):
    if depth > 20:
        return None
    if isinstance(node, dict):
        if 'Template' in node:
            return node['Template']
        for k, v in node.items():
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

template = find_template(d)
if template:
    print("Template keys:", list(template.keys()))
    print("Indicators:", template.get('Indicators', []))
    
    # Try adding LiveExporter to the main Indicators list
    if 'Indicators' in template and isinstance(template['Indicators'], list):
        le_entry = {
            "Type": "AtasExporter.LiveExporter, AtasExporter, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
            "Settings": json.dumps({
                "Name": "LiveExporter",
                "Visible": True
            })
        }
        template['Indicators'].append(json.dumps(le_entry))
        print(f"\nAdded LiveExporter to template.Indicators (now {len(template['Indicators'])} items)")
        
        with open(f, 'w', encoding='utf-8') as fh:
            json.dump(d, fh, indent=2, ensure_ascii=False)
        print("Workspace updated")
    else:
        print("No Indicators list in template")
else:
    print("Template not found")
