"""
Share-delete file reads for ATAS LiveExporter output.

Why this exists
---------------
The ATAS LiveExporter DLL (`atas-exporter/LiveExporter.cs`) publishes each
symbol's snapshot atomically: it serializes to `<file>.tmp`, then

    File.Move(tempPath, filePath, true);   // == MoveFileEx(MOVEFILE_REPLACE_EXISTING)

On Windows, `MoveFileEx` with REPLACE_EXISTING needs DELETE access on the
destination file. If ANY process holds an open handle on that file without
FILE_SHARE_DELETE, the rename fails with WinError 5 ("Access to the path is
denied"), the DLL's catch-all logs it, and that symbol's export silently stops
updating while every other symbol keeps streaming.

CPython's builtin `open()` on Windows opens with FILE_SHARE_READ|FILE_SHARE_WRITE
but NOT FILE_SHARE_DELETE. So the bridge's own `open(path)` calls were the thing
blocking the exporter — `bridge_server.py` was wedging its own data source.
Verified reproduction: hold the file with `open(p)`, then `os.replace(tmp, p)`
-> `PermissionError 5`.

Fix: read through a handle that grants FILE_SHARE_DELETE (plus read/write
sharing), so the DLL can always replace the file. Non-Windows and any API
failure fall back to the builtin open.
"""

import ctypes
import msvcrt
import os
import sys

_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x1
_FILE_SHARE_WRITE = 0x2
_FILE_SHARE_DELETE = 0x4
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_IS_WINDOWS = sys.platform == "win32"
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if _IS_WINDOWS else None

if _IS_WINDOWS:
    _KERNEL32.CreateFileW.restype = ctypes.c_void_p
    _KERNEL32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,      # lpFileName
        ctypes.c_uint32,       # dwDesiredAccess
        ctypes.c_uint32,       # dwShareMode
        ctypes.c_void_p,       # lpSecurityAttributes
        ctypes.c_uint32,       # dwCreationDisposition
        ctypes.c_uint32,       # dwFlagsAndAttributes
        ctypes.c_void_p,       # hTemplateFile
    ]
    _KERNEL32.GetLastError.restype = ctypes.c_uint32


def _read_shared_delete(path):
    """Read `path` via a FILE_SHARE_DELETE handle. Returns str, or None on failure."""
    if _KERNEL32 is None:
        return None
    handle = _KERNEL32.CreateFileW(
        path,
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL | _FILE_FLAG_SEQUENTIAL_SCAN,
        None,
    )
    if handle is None or handle == _INVALID_HANDLE_VALUE:
        return None
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    except OSError:
        # CRT refused the handle; we still own it, so close it directly.
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    # open_osfhandle transferred ownership of the handle to `fd`; closing the
    # fd closes the handle. If os.fdopen fails, close the fd to release it.
    try:
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read()
    except BaseException:
        return None
    # .NET writes UTF-8; utf-8-sig tolerates either a BOM or no BOM.
    return raw.decode("utf-8-sig", errors="replace")


def read_text(path, attempts=2):
    """Read `path` as text without ever blocking the exporter's atomic rename.

    Never returns None on the normal path — falls back to the builtin open so
    this is a drop-in replacement for `open(path).read()`. The only way to get
    an exception out is an unreadable/missing file, same as before.
    """
    if _IS_WINDOWS:
        for i in range(attempts):
            content = _read_shared_delete(path)
            if content is not None:
                return content
            if i + 1 < attempts:
                # Transient share/AV/backup-scanner hold — brief backoff.
                import time
                time.sleep(0.05 * (i + 1))
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()