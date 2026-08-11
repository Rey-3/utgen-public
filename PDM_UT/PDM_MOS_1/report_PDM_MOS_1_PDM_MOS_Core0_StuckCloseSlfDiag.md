# UTGen report: PDM_MOS_Core0_StuckCloseSlfDiag

- Function: `PDM_MOS_Core0_StuckCloseSlfDiag`
- File: ``
- Test cases: 10
- Coverage: 9 decisions; Coverage=1, MC/DC=9

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = True; decision 2 = False; decision 2 = True | — |
| 2 | MC/DC | decision 1 = False | PDM_Core0_StuckCloseStat[13].Stat=0, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |
| 3 | MC/DC | decision 1 = False | PDM_Core0_StuckCloseStat[13].Stat=0, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |
| 4 | MC/DC | decision 3 = True; decision 4 = True; decision 5 = True; decision 6 = False; decision 6 = True; decision 7 = False; decision 7 = True | — |
| 5 | MC/DC | decision 3 = False | PDM_Core0_StuckCloseStat[13].Stat=0, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |
| 6 | MC/DC | decision 3 = False | PDM_Core0_StuckCloseStat[13].Stat=0, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |
| 7 | MC/DC | decision 4 = False; decision 8 = False; decision 9 = False; decision 9 = True | — |
| 8 | MC/DC | decision 5 = False | PDM_Core0_StuckCloseStat[13].Stat=0, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |
| 9 | MC/DC | decision 8 = True | PDM_Core0_StuckCloseStat[13].Stat=1, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |
| 10 | Coverage | concrete decision 9 = True | PDM_Core0_StuckCloseStat[13].Stat=1, PDM_Core0_StuckCloseStat[13].FaultCnt=0, PDM_Core0_StuckCloseStat[13].RecoveryCnt=0 |

## Infeasible pairs
None.

## Uncontrollable conditions
- decision 2: uncontrollable or degenerate condition
- decision 6: uncontrollable or degenerate condition
- decision 7: uncontrollable or degenerate condition
- decision 9: uncontrollable or degenerate condition

## Manual actions
- decision 2: intent mismatch
- decision 7: intent mismatch
- decision 9: intent mismatch
