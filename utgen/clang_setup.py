from __future__ import annotations

import os
import sys
from pathlib import Path

from clang.cindex import Config

DEFAULT_LIBCLANG = r"C:\Program Files\LLVM\bin\libclang.dll"
_configured = False


def configure_libclang(path: str | None = None) -> None:
    """ Configure clang.cindex to use the libclang DLL"""
    global _configured

    # libclang only needs to be configured once for the current process.
    if _configured:
        return

    bundle_dir = getattr(sys, "_MEIPASS", None)
    bundled = [] if not bundle_dir else [
        Path(bundle_dir) / "clang" / "bin" / "libclang.dll",
        Path(bundle_dir) / "libclang.dll",
    ]


    candidate = Path(
        path
        or next((item for item in bundled if item.exists()), None)
        or os.environ.get("LIBCLANG_PATH")
        or DEFAULT_LIBCLANG
    )

    if candidate.is_dir():
        candidate = candidate / "libclang.dll"

    if not candidate.exists():
        raise FileNotFoundError(
            f"libclang not found: {candidate} (set 'libclang' in config.yaml or LIBCLANG_PATH)"
        )

    # Tell clang.cindex exactly which native libclang library should be loaded.
    Config.set_library_file(str(candidate))
    _configured = True
