# UTGen report: PDM_MOS_Core0_UpdateFireWarning_Handler

- Function: `PDM_MOS_Core0_UpdateFireWarning_Handler`
- File: ``
- Test cases: 42
- Coverage: 16 decisions; Coverage=8, MC/DC=31, Switch=3

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 2 | MC/DC | decision 1 = False; decision 2 = False; decision 3 = False; decision 4 = True; decision 6 = False; decision 7 = False; decision 11 = False; decision 15 = False; decision 16 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 3 | MC/DC | decision 1 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 4 | MC/DC | decision 2 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 5 | MC/DC | decision 2 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 6 | MC/DC | decision 3 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 7 | MC/DC | decision 3 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 8 | Switch | decision 4 = False; decision 7 = True; decision 9 = False; decision 15 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 9 | MC/DC | decision 5 = True; decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 10 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 11 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 12 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 13 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 14 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 15 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 16 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 17 | MC/DC | decision 5 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 18 | MC/DC | decision 5 = True | — |
| 19 | Coverage | concrete condition combination | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 20 | Coverage | concrete condition combination | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 21 | Coverage | concrete condition combination | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 22 | Coverage | concrete condition combination | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 23 | Coverage | concrete decision 5 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 24 | Coverage | concrete condition combination | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 25 | Coverage | concrete condition combination | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 26 | MC/DC | decision 6 = True | — |
| 27 | Coverage | concrete decision 6 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 28 | MC/DC | decision 8 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 29 | MC/DC | decision 8 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 30 | MC/DC | decision 8 = False; decision 9 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 31 | MC/DC | decision 9 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 32 | MC/DC | decision 10 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 33 | MC/DC | decision 10 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 34 | MC/DC | decision 10 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 35 | Switch | decision 11 = True; decision 12 = True; decision 13 = True; decision 15 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 36 | MC/DC | decision 12 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 37 | MC/DC | decision 13 = False | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 38 | MC/DC | decision 14 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 39 | MC/DC | decision 14 = False | — |
| 40 | MC/DC | decision 14 = False | — |
| 41 | Switch | decision 15 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |
| 42 | MC/DC | decision 16 = True | PDM_Core0_PrevPDMDtcClrEvent_PZCUFireWarningCnt=0 |

## Infeasible pairs
None.

## Uncontrollable conditions
- decision 5: uncontrollable or degenerate condition

## Manual actions
- decision 5: intent mismatch
- decision 6: intent mismatch
- decision 14: intent mismatch
- decision 14: intent mismatch
