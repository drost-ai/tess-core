from __future__ import annotations

import ctypes
import hashlib
import sys
from typing import Any


_MAX_PROCARGS_BYTES = 1024 * 1024


class _proc_bsd_info(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def inspect_darwin_process(pid: int, *, max_command_bytes: int) -> dict[str, Any]:
    """Read process birth, group, executable, and argv through native APIs."""
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libproc.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    libproc.proc_pidinfo.restype = ctypes.c_int
    libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    libproc.proc_pidpath.restype = ctypes.c_int

    info = _proc_bsd_info()
    returned = libproc.proc_pidinfo(
        pid,
        3,  # PROC_PIDTBSDINFO
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if returned != ctypes.sizeof(info):
        raise OSError(
            ctypes.get_errno(),
            "proc_pidinfo did not return PROC_PIDTBSDINFO",
        )

    path_buffer = ctypes.create_string_buffer(4096)
    path_bytes = libproc.proc_pidpath(pid, path_buffer, len(path_buffer))
    if path_bytes <= 0 or path_bytes >= len(path_buffer):
        raise OSError(
            ctypes.get_errno(),
            "proc_pidpath did not return a bounded executable path",
        )
    executable = bytes(path_buffer.raw[:path_bytes]).rstrip(b"\0")
    command = _read_command(libproc, pid, max_command_bytes)
    if info.pbi_pid != pid or info.pbi_pgid <= 0 or info.pbi_start_tvsec <= 0:
        raise ValueError("Darwin process metadata was incomplete")
    if not executable or len(executable) > max_command_bytes:
        raise ValueError("Darwin executable identity was invalid")
    return {
        "pid": pid,
        "processGroupId": int(info.pbi_pgid),
        "startToken": (
            f"darwin:{int(info.pbi_start_tvsec)}:{int(info.pbi_start_tvusec)}"
        ),
        "observedCommandDigest": hashlib.sha256(command).hexdigest(),
        "platform": sys.platform,
    }


def read_darwin_process_command(pid: int, *, max_command_bytes: int) -> bytes:
    """Read bounded NUL-delimited argv using the existing native parser."""
    return _read_command(ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True), pid, max_command_bytes)


def _read_command(libproc: Any, pid: int, max_command_bytes: int) -> bytes:
    libproc.sysctl.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    ]
    libproc.sysctl.restype = ctypes.c_int
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2, pid
    size = ctypes.c_size_t()
    if libproc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctl did not size KERN_PROCARGS2")
    if size.value <= ctypes.sizeof(ctypes.c_int) or size.value > _MAX_PROCARGS_BYTES:
        raise ValueError("KERN_PROCARGS2 size was outside the bounded contract")
    buffer = ctypes.create_string_buffer(size.value)
    if libproc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), "sysctl did not read KERN_PROCARGS2")
    raw = bytes(buffer.raw[: size.value])
    int_size = ctypes.sizeof(ctypes.c_int)
    argc = int.from_bytes(raw[:int_size], byteorder=sys.byteorder, signed=True)
    if argc <= 0 or argc > 4096:
        raise ValueError("KERN_PROCARGS2 argc was invalid")

    offset = raw.find(b"\0", int_size)
    if offset < 0:
        raise ValueError("KERN_PROCARGS2 executable was unterminated")
    offset += 1
    while offset < len(raw) and raw[offset] == 0:
        offset += 1

    argv: list[bytes] = []
    command_bytes = 0
    for _ in range(argc):
        end = raw.find(b"\0", offset)
        if end < 0:
            raise ValueError("KERN_PROCARGS2 argv was unterminated")
        argument = raw[offset:end]
        command_bytes += len(argument) + 1
        if command_bytes > max_command_bytes:
            raise ValueError("KERN_PROCARGS2 argv exceeded the command bound")
        argv.append(argument)
        offset = end + 1
    return b"\0".join(argv) + b"\0"
