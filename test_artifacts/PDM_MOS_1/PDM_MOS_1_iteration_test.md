# PDM_MOS_1 iteration test

Target path: `C:\Users\zhibo\Desktop\ddman\liauto_pdc\Application\app_source\AppSWC\PDM\PDM_MOS_1.c`

The source repository was used only through the supplied path. Its existing `Application/pf_source/src` modification was left untouched.

## Results

1. First generation on `SWP10.60`
   - 50 functions and 655 active tool rows were generated.
   - 62 active stubs were recorded.
   - One module JSON, one Analysis DB XML, and 50 function workbooks were written.
   - No Value XML or Stub XML was generated.

2. Manual workbook edits
   - Changed `PDM_GetSeqIndexByLoadId` row `PDM_MOS_1_016` `LoadIndex` from `127` to `70u`.
   - Added a manual row with `0x2Au` input and `0x01u` output.
   - Changed one function stub body from `{0}` to `{1}`.

3. Manual User Merge
   - Result: `changed=1`, `added=1`, `stubs changed=1`.
   - The changed row and manual row are user-owned and active.
   - The manual row received never-reused ID `PDM_MOS_1_656`.

4. Tool Iteration on `SWP11.52`
   - 52 functions, 749 rows, and 64 active stubs are present.
   - 93 new row IDs were allocated and 7 obsolete rows became orphaned.
   - New functions: `PDM_MOS_Core0_DtcClear_Handler`, `PDM_MOS_Core0_LightInd_HandleStuckCloseFault_BrkHighBrk`.
   - New stubs: `Rte_PDMMOS_1_DtcApp_PDMDtcClrEventCore0ASILB_Event_GetMsg`, `Rte_PDMMOS_1_PDMProtectionCtrl_1_ShortcurRecoverRetryInfoCore0ASILB_1ms_GetMsg`.
   - The manual `70u`, `0x2Au`, `0x01u`, and `{1}` values remained unchanged.

## Verification

- JSON validation passed through `load_module`.
- Analysis DB XML parsed successfully and contains 52 functions.
- All 52 workbooks passed General identity and workbook schema validation.
- No Value XML or Stub XML exists in the test output.
- Excel files were intentionally not staged.
