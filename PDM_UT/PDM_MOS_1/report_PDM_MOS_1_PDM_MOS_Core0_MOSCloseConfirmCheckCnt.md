# UTGen report: PDM_MOS_Core0_MOSCloseConfirmCheckCnt

- Function: `PDM_MOS_Core0_MOSCloseConfirmCheckCnt`
- File: ``
- Test cases: 3
- Coverage: 2 decisions; MC/DC=3

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False | PDM_Core0_MOSCloseConfirmCnt[13]=0 |
| 2 | MC/DC | decision 1 = True; decision 2 = True | PDM_Core0_MOSCloseConfirmCnt[13]=0 |
| 3 | MC/DC | decision 2 = False | PDM_Core0_MOSCloseConfirmCnt[13]=50 |

## Infeasible pairs
None.

## Uncontrollable conditions
None.

## Manual actions
None.
