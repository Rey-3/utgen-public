# UTGen report: PDM_MOS_Core0_IsStuckCloseRecoverOK

- Function: `PDM_MOS_Core0_IsStuckCloseRecoverOK`
- File: ``
- Test cases: 7
- Coverage: 4 decisions; Coverage=1, MC/DC=6

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = True | PDM_MOS_Core0_IsStuckCloseRecoverOK()=0 |
| 2 | MC/DC | decision 1 = False | PDM_MOS_Core0_IsStuckCloseRecoverOK()=0 |
| 3 | MC/DC | decision 1 = False | PDM_MOS_Core0_IsStuckCloseRecoverOK()=0 |
| 4 | MC/DC | decision 2 = False | PDM_MOS_Core0_IsStuckCloseRecoverOK()=0 |
| 5 | MC/DC | decision 2 = True; decision 3 = True; decision 4 = False; decision 4 = True | PDM_MOS_Core0_IsStuckCloseRecoverOK()=1 |
| 6 | MC/DC | decision 3 = False | PDM_MOS_Core0_IsStuckCloseRecoverOK()=1 |
| 7 | Coverage | concrete decision 4 = False | PDM_MOS_Core0_IsStuckCloseRecoverOK()=0 |

## Infeasible pairs
None.

## Uncontrollable conditions
- decision 4: uncontrollable or degenerate condition

## Manual actions
None.
