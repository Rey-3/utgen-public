# -*- mode: python ; coding: utf-8 -*-
import glob
import os

from PyInstaller.utils.hooks import collect_submodules


block_cipher = None
here = os.path.abspath(SPECPATH)
llvm_root = os.environ.get("UTGEN_LLVM_ROOT", r"C:\Program Files\LLVM")
clang_exe = os.path.join(llvm_root, "bin", "clang.exe")
libclang = os.path.join(llvm_root, "bin", "libclang.dll")

if not os.path.exists(clang_exe) or not os.path.exists(libclang):
    raise SystemExit(f"LLVM toolchain not found under {llvm_root!r}")

resource_dirs = sorted(glob.glob(os.path.join(llvm_root, "lib", "clang", "*")))
if not resource_dirs:
    raise SystemExit(f"LLVM resource headers not found under {llvm_root!r}")

version = os.path.basename(resource_dirs[-1])
binaries = [(clang_exe, "clang/bin"), (libclang, "clang/bin")]
datas = [(resource_dirs[-1], os.path.join("clang", "lib", "clang", version)),
         (os.path.join(here, "template.xlsx"), ".")]
hiddenimports = collect_submodules("utgen") + collect_submodules("clang")

a = Analysis(
    [os.path.join(here, "utgen.py")],
    pathex=[here],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="utgen",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
