# UTGen — C Unit-Test Vector Generator

Static analysis of C functions with clang.cindex that auto-generates row-aligned
test vectors — statement / branch / decision / MC/DC coverage, boundary tests and
fault injection — for the TESSY pipeline. The current CLI uses JSON module state
and exports TESSY Excel workbooks.

Configure preprocessor macros for all modules in `config.yaml` under
`settings.defines`; no Python code changes are needed:

```yaml
settings:
  defines:
    - __GHS__
    - USE_CANTRV_TJA1145
    - __CCRH__
    - USE_IOC
    - USE_PMIC_FS26
```

Entries also accept `NAME=VALUE` or `-DNAME=VALUE`. Omit `defines` or use `[]`
to add no macros. Changing this list invalidates the generation cache.

## Documents

- Spec: `docs/superpowers/specs/2026-07-21-utgen-design.md`
- Implementation plan: `docs/superpowers/plans/2026-07-21-utgen-implementation.md`
- Progress log: `docs/superpowers/plans/PROGRESS.md`

## Windows runtime MC/DC requirements

Install Python dependencies with `python -m pip install -r requirements.txt`.

On Windows, run `./build_generate_from_config.ps1` to build `dist/utgen.exe`.
The build script installs PyInstaller and copies `config.yaml` beside the executable.
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
The runtime script finds these tools on `PATH`; add the LLVM `bin` directory
before running it.

Open **x64 Native Tools Command Prompt for VS 2022**, go to this project,
and run:

```bat
python tests/runtime_mcdc_pdm.py
```

This runs 18 PDM targets in `tests/c_samples/pdm_mos_mcdc_targets.c`,
13 PDC/PNG/NmApp targets in `tests/c_samples/other_modules_mcdc_targets.c`,
and two additional targets for sequential closing and sleep conditions.
The examples include their real decision helpers; only external RTE calls are
stubbed. Local static inputs are injected through temporary C copies under
`generated/llvm_mcdc`, leaving the example bodies intact.

The script requires **100% branch coverage** for every target and **100% MC/DC
coverage** where LLVM reports MC/DC conditions. Targets with only single-condition
decisions have no MC/DC goals. A coverage failure after successful compilation means the
generated test inputs leave coverage gaps; reinstalling build tools will not
resolve those gaps.

Setup references: [Microsoft C++ Build Tools installation](https://learn.microsoft.com/en-us/cpp/overview/acquire-msvc?view=msvc-170)
and [LLVM MC/DC documentation](https://releases.llvm.org/19.1.0/tools/clang/docs/SourceBasedCodeCoverage.html#mcdc-instrumentation).

## Status

Under development, one task per run (see progress log).
Full install/usage documentation arrives with Task 17.
