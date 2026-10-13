import json, os

appdata = os.environ.get('APPDATA', '')
# Look at a simpler workspace
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SMART TAPE.aws')
workspace_name = 'SMART TAPE.aws'

if not os.path.exists(f):
    f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'solusdc.aws')
    workspace_name = 'solusdc.aws'

print(f"Reading: {workspace_name}")
with open(f, encoding='utf-8') as fh:
    data = json.load(fh)

def find_all_indicators(obj, path=''):
    results = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if k == 'SerializedIndicators' and isinstance(v, list):
                for i, si in enumerate(v):
                    try:
                        parsed = json.loads(si)
                        ind_type = parsed.get('Type', '?')
                        settings = parsed.get('Settings', '{}')
                        try:
                            sets = json.loads(settings)
                            name = sets.get('Name', sets.get('DisplayName', ''))
                        except:
                            name = ''
                        results.append((f"{new_path}[{i}]", ind_type, name))
                    except:
                        results.append((f"{new_path}[{i}]", si[:100], ''))
            elif isinstance(v, (dict, list)):
                results.extend(find_all_indicators(v, new_path))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            results.extend(find_all_indicators(item, f"{path}[{i}]"))
    return results

indicators = find_all_indicators(data)
for path, ind_type, name in indicators:
    print(f"  [{path}]")
    print(f"    Type: {ind_type}")
    if name:
        print(f"    Name: {name}")
    print()
