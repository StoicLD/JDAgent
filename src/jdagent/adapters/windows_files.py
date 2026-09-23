"""Exclusive Windows file handles for bounded local text operations.

Readers and writers cannot overlap this handle. Interrupted writes are unknown,
not rolled back; callers must persist execution-start before entering this seam.
"""

import ctypes
import os
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO


@contextmanager
def locked_ancestors(root: Path, target: Path) -> Generator[None, None, None]:
    """Prevent ancestor replacement and reparse writes during a file operation."""
    target.relative_to(root)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    ancestors = [root]
    for component in target.parent.relative_to(root).parts:
        ancestors.append(ancestors[-1] / component)
    with ExitStack() as stack:
        for directory in ancestors:
            handle = create(str(directory), 0x80, 1, None, 3, 0x02200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            stack.callback(close, handle)
            if directory.lstat().st_file_attributes & 0x400:
                raise OSError("Reparse ancestors are unsupported")
        yield


@contextmanager
def locked_file(
    path: Path, *, create: bool = False, write: bool = False
) -> Generator[BinaryIO, None, None]:
    """Open a non-shared regular file; never truncate before version validation."""
    if os.name != "nt":
        raise OSError("This file adapter requires Windows")
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0xC0000000 if write else 0x80000000,
        0,
        None,
        1 if create else 3,
        0x00200000,
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
    except BaseException:
        close(handle)
        raise
    with os.fdopen(fd, "r+b" if write else "rb") as stream:
        status = os.fstat(stream.fileno())
        if status.st_nlink != 1 or status.st_file_attributes & 0x400:
            raise OSError("Linked or reparse targets are unsupported")
        yield stream
