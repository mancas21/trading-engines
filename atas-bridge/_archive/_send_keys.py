import win32gui, win32con, time

# Find ATAS X main window
hwnds = []
def enum_windows(hwnd, results):
    if win32gui.IsWindowVisible(hwnd):
        title = win32gui.GetWindowText(hwnd)
        if title == 'ATAS X':
            results.append(hwnd)
    return True
win32gui.EnumWindows(enum_windows, hwnds)

if hwnds:
    hwnd = hwnds[0]
    print(f"Found ATAS X: HWND={hwnd}")
    
    # Bring to front
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    
    # Try Alt+I (Indicators menu)
    print("Sending Alt+I...")
    win32gui.PostMessage(hwnd, win32con.WM_SYSKEYDOWN, ord('I'), 0)
    time.sleep(0.3)
    win32gui.PostMessage(hwnd, win32con.WM_SYSKEYUP, ord('I'), 0)
    time.sleep(1)
    
    # Try 'A' for Add
    print("Sending 'A'...")
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, ord('A'), 0)
    time.sleep(0.1)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, ord('A'), 0)
    time.sleep(2)
    
    # Try typing LiveExporter
    print("Typing LiveExporter...")
    for c in "LiveExporter":
        win32gui.PostMessage(hwnd, win32con.WM_CHAR, ord(c), 0)
        time.sleep(0.05)
    time.sleep(1)
    
    # Enter
    print("Sending Enter...")
    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
    time.sleep(0.1)
    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
    time.sleep(2)
    
    print("Done")
else:
    print("ATAS X window not found")
