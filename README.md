# UTGen — C Unit-Test Vector Generator

Static analysis of C functions with clang.cindex that auto-generates row-aligned
test vectors — statement / branch / decision / MC/DC coverage, boundary tests and
fault injection — for the TESSY pipeline (consumed via tessy_parse.py).

## Documents

- Spec: `docs/superpowers/specs/2026-07-21-utgen-design.md`
- Implementation plan: `docs/superpowers/plans/2026-07-21-utgen-implementation.md`
- Progress log: `docs/superpowers/plans/PROGRESS.md`

## Windows runtime MC/DC requirements

Install Python dependencies with `python -m pip install -r requirements.txt`.
The C runtime test also requires these separately installed tools:

- [Download Visual Studio 2022 Build Tools](https://aka.ms/vs/17/release/vs_BuildTools.exe).
  Run the installer, select **Desktop development with C++**, and include
  **MSVC x64/x86 build tools** and a **Windows SDK**.
  If these are already installed, no new download is needed.
- [Download LLVM 19.1.7 for Windows x64](https://github.com/llvm/llvm-project/releases/download/llvmorg-19.1.7/LLVM-19.1.7-win64.exe).
  Runtime MC/DC requires LLVM **19 or newer** because the source contains
  expressions with more than six conditions. Use `clang`, `llvm-profdata`,
  and `llvm-cov` from the same LLVM release.

The Python `clang==18.1.8` package in `requirements.txt` provides bindings
for source analysis; it does not install the LLVM compiler or Windows SDK.
The runtime script prefers tools in `.utgen/llvm19/bin`, then searches `PATH`.

Open **x64 Native Tools Command Prompt for VS 2022**, go to this project,
and run:

```bat
python tests/runtime_mcdc_pdm.py
```

The script requires **100% branch coverage and 100% MC/DC coverage** for each
target function. A coverage failure after successful compilation means the
generated test inputs leave coverage gaps; reinstalling build tools will not
resolve those gaps.

Setup references: [Microsoft C++ Build Tools installation](https://learn.microsoft.com/en-us/cpp/overview/acquire-msvc?view=msvc-170)
and [LLVM MC/DC documentation](https://releases.llvm.org/19.1.0/tools/clang/docs/SourceBasedCodeCoverage.html#mcdc-instrumentation).

## Status

Under development, one task per run (see progress log).
Full install/usage documentation arrives with Task 17.
