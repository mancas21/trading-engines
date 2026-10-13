import json, os

f = os.path.join(os.environ['APPDATA'], 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')
with open(f, encoding='utf-8') as fh:
    data = json.load(fh)

def find_key(obj, target, path='', depth=0):
    if depth > 15:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_path = f"{path}.{k}" if path else k
            if target.lower() in k.lower():
                print(f"{new_path}: {json.dumps(v)[:200]}")
            if isinstance(v, (dict, list)):
                find_key(v, target, new_path, depth+1)
    elif isinstance(obj, list):
        for i, item in enumerate(obj[:10]):
            new_path = f"{path}[{i}]"
            if isinstance(item, (dict, list)):
                find_key(item, target, new_path, depth+1)

# Search for indicator-related keys
for target in ['Indicator', 'Study', 'Export', 'Live', 'Exporter']:
    print(f"\n=== Searching for '{target}' ===")
    find_key(data, target)
