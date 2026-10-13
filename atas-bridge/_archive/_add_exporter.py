try:
    # Method 1: Try to add LiveExporter to workspace file
    import json, os
    
    appdata = os.environ.get('APPDATA', '')
    f = os.path.join(appdata, 'ATAS X', 'Workspaces2', 'SOLUSDT SMART TAPE.aws')
    
    with open(f, 'r', encoding='utf-8') as fh:
        data = json.load(fh)
    
    def find_all_panels(node, depth=0):
        results = []
        if depth > 20:
            return results
        if isinstance(node, dict):
            if 'SerializedIndicators' in node:
                results.append(node)
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    results.extend(find_all_panels(v, depth+1))
        elif isinstance(node, list):
            for item in node:
                results.extend(find_all_panels(item, depth+1))
        return results
    
    panels = find_all_panels(data)
    
    # Add to first panel (DOM panel = main chart)
    exporter_entry = {
        "Type": "AtasExporter.LiveExporter, AtasExporter, Version=1.0.0.0, Culture=neutral, PublicKeyToken=null",
        "Settings": json.dumps({
            "Name": "LiveExporter",
            "DisplayName": "LiveExporter",
            "Enabled": True
        })
    }
    
    panels[0]['SerializedIndicators'].append(json.dumps(exporter_entry))
    
    with open(f, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    
    print(f"✅ LiveExporter added to workspace!")
    print("Restart ATAS X for changes to take effect.")
    
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()
