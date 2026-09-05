# Task 1 报告

## 修改文件

- `utgen/iteration/__init__.py`
- `utgen/iteration/adapters/__init__.py`
- `utgen/iteration/adapters/analysis_db.py`
- `utgen/iteration/adapters/tessy_workbook.py`
- `utgen/assembler.py`
- `utgen/boundary.py`
- `utgen/coverage.py`
- `utgen/fault.py`
- `utgen/model.py`
- `tests/test_assembler.py`
- 本报告文件

`tests/test_pr07_workbook_adapter.py` 未需改动。`generation_times.txt` 是已有工作区修改，未暂存或提交；未修改 `.utgen`。

## 设计决定

- iteration package 现在只导出保留的 Analysis DB 与 TESSY workbook adapter；删除对已删除 iteration domain、XML adapter、fingerprint 与 versioning 模块的悬空导入。
- `CellValue` 与 `ExcelColumnKey` 移入 workbook adapter；Analysis DB 的 timestamp 规范化与 metadata 校验在 adapter 内自包含实现。
- `RowAssembler.assemble()` 保持原有生成循环、dedup、顺序与 `TestRow` concrete values；新增 `assemble_with_provenance()` 复用同一私有 `_assemble()`。
- provenance 使用三个 frozen dataclass。每个 Decision recipe 记录 source-order index、Decision/Atom 内容、完整布尔值、结果及 concrete row index；dedup 时仍追加指向已有 concrete row 的 recipe。coverage repair 能取得完整 Atom pattern 时归入 recipe。
- 无 Decision 的 nominal row 记录 `GeneratedRowProvenance("Nominal", 0, 0)`。timeout fallback 仍由后续 generator workflow Task 接入，因为本 Task 授权文件不包含 `generate_from_config.py`。
- 删除 assembler、boundary、coverage、fault 中仅服务旧 persistence identity 的 obligation-key 构造与 fingerprint 运行时依赖；保留 `TestRow` 既有字段以避免扩大本 Task 的 caller 变更范围。

## RED / GREEN

RED（新增测试后、production provenance 接口尚未完成）：

```text
.\.venv\Scripts\python.exe -m pytest tests/test_assembler.py -k provenance -q
ModuleNotFoundError: No module named 'utgen.iteration.fingerprint'
```

该失败由 legacy assembler 的 deleted-identity 运行时依赖触发，确认目标清理与新 provenance 行为尚未完成。

GREEN：

```text
.\.venv\Scripts\python.exe -m pytest tests/test_assembler.py -k provenance -q
3 passed, 33 deselected in 0.20s
```

## 验证

- `pytest tests/test_pr07_workbook_adapter.py -q`: `8 passed, 2 failed`；两个失败均因基线已删除的 `input/PDM_MOS_1/*.xlsx` fixture 不存在。
- 核心命令 `pytest tests/test_assembler.py tests/test_mcdc.py tests/test_solver.py tests/test_interp.py tests/test_pr07_workbook_adapter.py -q`: `75 passed, 16 failed`；失败为基线 parser/interpreter/coverage 预期不一致及上述两个缺失 fixture，不涉及本 Task 新测试。
- `python -c "import utgen.iteration"`: 通过。
- `python -m compileall -q utgen`: 通过。
- `git diff --check`: 通过（仅显示已有文件的换行转换 warning，无 whitespace error）。

## Self-review

- 未新增依赖；未恢复旧 revision/XML/identity/fingerprint 架构；没有复制生成算法。
- concrete-row 回归断言 `result.rows == tuple(legacy)`，并覆盖 deduplicated row 保留多个 logical recipes 与 nominal provenance。
- 提交前将显式检查 status，并仅暂存本 Task 文件和本报告，排除 `generation_times.txt`。

## 未解决问题

- PR07 的真实与 negative workbook fixture 已不在当前基线，无法在本 Task 范围内恢复或修改测试掩盖。
- 核心回归中的 14 个非 fixture 失败属于当前基线 parser/interpreter/coverage 行为，不是本 Task 的授权改动范围。
- timeout provenance 需要后续授权修改 `generate_from_config.py` 时，将 `fallback_rows()` 的两行接入 `GeneratedRowProvenance("Timeout", source_order, concrete_row_index)`。
