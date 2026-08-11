# UTGen report: PDM_MOS_Core0_HandleStuckCloseFault

- Function: `PDM_MOS_Core0_HandleStuckCloseFault`
- File: ``
- Test cases: 2
- Coverage: 1 decisions; MC/DC=2

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False | PDM_Core0_StuckCloseStat[13].Stat=0, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |
| 2 | MC/DC | decision 1 = True | PDM_Core0_StuckCloseStat[13].Stat=0, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |

## Infeasible pairs
None.

## Uncontrollable conditions
None.

## Manual actions
None.
