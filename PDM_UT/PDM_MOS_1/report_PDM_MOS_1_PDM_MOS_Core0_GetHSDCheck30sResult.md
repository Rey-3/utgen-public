# UTGen report: PDM_MOS_Core0_GetHSDCheck30sResult

- Function: `PDM_MOS_Core0_GetHSDCheck30sResult`
- File: ``
- Test cases: 3
- Coverage: 2 decisions; Coverage=1, MC/DC=2

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False | PDM_MOS_Core0_GetHSDCheck30sResult()=0 |
| 2 | MC/DC | decision 1 = True; decision 2 = False; decision 2 = True | — |
| 3 | Coverage | concrete decision 2 = True | PDM_MOS_Core0_GetHSDCheck30sResult()=1 |

## Infeasible pairs
None.

## Uncontrollable conditions
- decision 2: uncontrollable or degenerate condition

## Manual actions
- decision 2: intent mismatch
