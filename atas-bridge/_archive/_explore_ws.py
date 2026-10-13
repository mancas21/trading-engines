import json, os, time

appdata = os.environ.get('APPDATA', '')
f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')

with open(f, 'r', encoding='utf-8') as fh:
    data = json.load(fh)

def explore(obj, depth=0, max_depth=8):
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == 'Indicators' and isinstance(v, list):
                print(f"{'  '*depth}Indicators ({len(v)}):")
                for ind in v:
                    print(f"{'  '*depth}  - {ind.get('Name','?')} Type={ind.get('Type','?')} Enabled={ind.get('Enabled','?')}")
            if k == 'Parameter' and isinstance(v, dict) and 'Indicator' in v:
                ind = v['Indicator']
                print(f"{'  '*depth}Indicator: {ind.get('Name','?')} Type={ind.get('Type','?')}")
            if isinstance(v, (dict, list)):
                explore(v, depth+1, max_depth)
    elif isinstance(obj, list):
        for item in obj[:5]:
            explore(item, depth, max_depth)

explore(data)
print("--- done ---")
