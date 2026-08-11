# UTGen report: PDM_MOS_Core0_IsStuckCloseFaultValid

- Function: `PDM_MOS_Core0_IsStuckCloseFaultValid`
- File: ``
- Test cases: 8
- Coverage: 5 decisions; Coverage=1, MC/DC=7

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False | PDM_MOS_Core0_IsStuckCloseFaultValid()=0 |
| 2 | MC/DC | decision 1 = True; decision 2 = True | PDM_MOS_Core0_IsStuckCloseFaultValid()=0 |
| 3 | MC/DC | decision 2 = False | PDM_MOS_Core0_IsStuckCloseFaultValid()=0 |
| 4 | MC/DC | decision 2 = False | PDM_MOS_Core0_IsStuckCloseFaultValid()=0 |
| 5 | MC/DC | decision 3 = False | PDM_MOS_Core0_IsStuckCloseFaultValid()=0 |
| 6 | MC/DC | decision 3 = True; decision 4 = True; decision 5 = False; decision 5 = True | — |
| 7 | MC/DC | decision 4 = False | PDM_MOS_Core0_IsStuckCloseFaultValid()=0 |
| 8 | Coverage | concrete decision 5 = True | PDM_MOS_Core0_IsStuckCloseFaultValid()=1 |

## Infeasible pairs
None.

## Uncontrollable conditions
- decision 5: uncontrollable or degenerate condition

## Manual actions
- decision 5: intent mismatch
