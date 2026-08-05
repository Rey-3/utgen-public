"""libclang discovery: explicit path -> LIBCLANG_PATH env -> default LLVM install."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from clang.cindex import Config

DEFAULT_LIBCLANG = r"C:\Program Files\LLVM\bin\libclang.dll"
_configured = False


def configure_libclang(path: str | None = None) -> None:
    """Point clang.cindex at a libclang.dll once; later calls are no-ops.

    libclang can only be loaded once per process, hence the latch. The CLI
    configures from config.yaml before any parse; tests fall through to the
    default LLVM install.
    """
    global _configured
    if _configured:
        return
    bundle_dir = getattr(sys, "_MEIPASS", None)
    bundled = [] if not bundle_dir else [Path(bundle_dir) / "clang" / "bin" / "libclang.dll",
                                          Path(bundle_dir) / "libclang.dll"]
    candidate = Path(path or next((item for item in bundled if item.exists()),
                                  os.environ.get("LIBCLANG_PATH") or DEFAULT_LIBCLANG))
    if candidate.is_dir():
        candidate = candidate / "libclang.dll"
    if not candidate.exists():
        raise FileNotFoundError(
            f"libclang not found: {candidate} (set 'libclang' in config.yaml or LIBCLANG_PATH)")
    Config.set_library_file(str(candidate))
    _configured = True
