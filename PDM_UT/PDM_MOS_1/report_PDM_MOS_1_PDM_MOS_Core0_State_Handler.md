# UTGen report: PDM_MOS_Core0_State_Handler

- Function: `PDM_MOS_Core0_State_Handler`
- File: ``
- Test cases: 12
- Coverage: 8 decisions; Coverage=4, MC/DC=8

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False; decision 1 = True; decision 2 = True | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 2 | MC/DC | decision 2 = False | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 3 | MC/DC | decision 2 = True; decision 3 = True; decision 3 = False; decision 4 = False; decision 4 = True; decision 5 = False; decision 6 = False; decision 6 = True; decision 7 = False; decision 8 = True | — |
| 4 | MC/DC | decision 3 = False | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 5 | Coverage | concrete condition combination | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 6 | Coverage | concrete decision 4 = True | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 7 | MC/DC | decision 5 = True; decision 8 = False | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 8 | MC/DC | decision 7 = True | PDM_Core0_MOSCloseConfirmCnt[13]=50, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 9 | MC/DC | decision 8 = True | — |
| 10 | MC/DC | decision 8 = True | — |
| 11 | Coverage | concrete condition combination | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=0, PDM_Core0_MOSOpenConfirmCnt1[13]=50, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |
| 12 | Coverage | concrete condition combination | PDM_Core0_MOSCloseConfirmCnt[13]=0, PDM_Core0_MOSOpenConfirmCnt2[13]=50, PDM_Core0_MOSOpenConfirmCnt1[13]=0, PDM_Core0_MosStatInfo.LoadMosStateResult[13]=0 |

## Infeasible pairs
None.

## Uncontrollable conditions
- decision 1: uncontrollable or degenerate condition
- decision 3: uncontrollable or degenerate condition
- decision 4: uncontrollable or degenerate condition
- decision 6: uncontrollable or degenerate condition

## Manual actions
- decision 4: intent mismatch
- decision 8: intent mismatch
- decision 8: intent mismatch
