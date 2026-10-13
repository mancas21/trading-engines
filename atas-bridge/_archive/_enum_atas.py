import win32gui, win32con

# Find ATAS X window
def enum_windows_proc(hwnd, results):
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        class_name = win32gui.GetClassName(hwnd)
        if 'ATAS X' in title or 'ATAS' in title:
            results.append((hwnd, title, class_name))
    return True

results = []
win32gui.EnumWindows(enum_windows_proc, results)

print("Found windows:")
for hwnd, title, cls in results:
    print(f"  HWND={hwnd} Title='{title}' Class='{cls}'")
    # Get child windows
    def enum_child(hwnd, children):
        children.append((hwnd, win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd)))
        return True
    children = []
    win32gui.EnumChildWindows(hwnd, enum_child, children)
    print(f"    Children ({len(children)}):")
    for chwnd, ccls, ctitle in children[:10]:
        print(f"      HWND={chwnd} Class='{ccls}' Text='{ctitle}'")
