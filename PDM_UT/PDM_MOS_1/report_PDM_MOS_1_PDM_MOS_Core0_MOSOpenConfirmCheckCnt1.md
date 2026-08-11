# UTGen report: PDM_MOS_Core0_MOSOpenConfirmCheckCnt1

- Function: `PDM_MOS_Core0_MOSOpenConfirmCheckCnt1`
- File: ``
- Test cases: 3
- Coverage: 2 decisions; MC/DC=3

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False | PDM_Core0_MOSOpenConfirmCnt1[13]=0 |
| 2 | MC/DC | decision 1 = True; decision 2 = True | PDM_Core0_MOSOpenConfirmCnt1[13]=0 |
| 3 | MC/DC | decision 2 = False | PDM_Core0_MOSOpenConfirmCnt1[13]=50 |

## Infeasible pairs
None.

## Uncontrollable conditions
None.

## Manual actions
None.
