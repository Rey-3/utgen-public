# UTGen report: PDM_MOS_Core0_SequentialClose_Handler

- Function: `PDM_MOS_Core0_SequentialClose_Handler`
- File: ``
- Test cases: 20
- Coverage: 11 decisions; Coverage=1, MC/DC=19

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False | PDM_Core0_SequentialCloseStat[3].Load_id=0, PDM_Core0_SequentialCloseStat[3].Load_id_Other1=0, PDM_Core0_SequentialCloseStat[3].Load_id_Other2=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimer=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimeFlg=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 2 | MC/DC | decision 1 = True; decision 2 = False; decision 2 = True; decision 2 = True; decision 2 = True; decision 2 = True; decision 2 = True; decision 5 = False | — |
| 3 | MC/DC | decision 1 = True | PDM_Core0_SequentialCloseStat[3].Load_id=13, PDM_Core0_SequentialCloseStat[3].Load_id_Other1=0, PDM_Core0_SequentialCloseStat[3].Load_id_Other2=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimer=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimeFlg=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 4 | MC/DC | decision 1 = True | PDM_Core0_SequentialCloseStat[3].Load_id=0, PDM_Core0_SequentialCloseStat[3].Load_id_Other1=13, PDM_Core0_SequentialCloseStat[3].Load_id_Other2=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimer=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimeFlg=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 5 | MC/DC | decision 1 = True | PDM_Core0_SequentialCloseStat[3].Load_id=0, PDM_Core0_SequentialCloseStat[3].Load_id_Other1=0, PDM_Core0_SequentialCloseStat[3].Load_id_Other2=13, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimer=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimeFlg=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 6 | Coverage | concrete condition combination | PDM_Core0_SequentialCloseStat[3].Load_id=1, PDM_Core0_SequentialCloseStat[3].Load_id_Other1=0, PDM_Core0_SequentialCloseStat[3].Load_id_Other2=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimer=0, PDM_Core0_SequentialCloseStat[3].ChangeToCloseTimeFlg=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 7 | MC/DC | decision 3 = True; decision 4 = True | — |
| 8 | MC/DC | decision 3 = False | — |
| 9 | MC/DC | decision 3 = False | — |
| 10 | MC/DC | decision 3 = False | — |
| 11 | MC/DC | decision 5 = True | — |
| 12 | MC/DC | decision 6 = True; decision 7 = False; decision 8 = False | — |
| 13 | MC/DC | decision 6 = False; decision 9 = True; decision 10 = False; decision 11 = False | — |
| 14 | MC/DC | decision 6 = False | — |
| 15 | MC/DC | decision 6 = False | — |
| 16 | MC/DC | decision 7 = True; decision 8 = True | — |
| 17 | MC/DC | decision 7 = False; decision 8 = False | — |
| 18 | MC/DC | decision 9 = False | — |
| 19 | MC/DC | decision 10 = True; decision 11 = True | — |
| 20 | MC/DC | decision 10 = False; decision 11 = False | — |

## Infeasible pairs
- decision 4: infeasible MC/DC row
- decision 4: infeasible MC/DC row
- decision 7: infeasible MC/DC row
- decision 8: infeasible MC/DC row

## Uncontrollable conditions
- decision 2: uncontrollable or degenerate condition

## Manual actions
- decision 2: intent mismatch
- decision 3: intent mismatch
- decision 3: intent mismatch
- decision 3: intent mismatch
- decision 3: intent mismatch
- decision 5: intent mismatch
- decision 6: intent mismatch
- decision 6: intent mismatch
- decision 6: intent mismatch
- decision 6: intent mismatch
- decision 7: intent mismatch
- decision 7: intent mismatch
- decision 9: intent mismatch
- decision 10: intent mismatch
- decision 10: intent mismatch
