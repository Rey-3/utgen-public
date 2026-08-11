# UTGen report: PDM_MOS_Core0_MOSOpenConfirmCheckCnt2

- Function: `PDM_MOS_Core0_MOSOpenConfirmCheckCnt2`
- File: ``
- Test cases: 4
- Coverage: 2 decisions; MC/DC=4

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = True; decision 2 = True | PDM_Core0_MOSOpenConfirmCnt2[13]=0 |
| 2 | MC/DC | decision 1 = False | PDM_Core0_MOSOpenConfirmCnt2[13]=0 |
| 3 | MC/DC | decision 1 = False | PDM_Core0_MOSOpenConfirmCnt2[13]=0 |
| 4 | MC/DC | decision 2 = False | PDM_Core0_MOSOpenConfirmCnt2[13]=50 |

## Infeasible pairs
None.

## Uncontrollable conditions
None.

## Manual actions
None.
