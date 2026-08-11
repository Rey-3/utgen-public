# UTGen report: PDM_MOS_Core0_PriorityResolve

- Function: `PDM_MOS_Core0_PriorityResolve`
- File: ``
- Test cases: 17
- Coverage: 23 decisions; Coverage=1, MC/DC=16

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False; decision 2 = False; decision 3 = False; decision 4 = False; decision 5 = False; decision 6 = False; decision 7 = False; decision 7 = True; decision 8 = False; decision 8 = True; decision 9 = False; decision 9 = True; decision 10 = False; decision 12 = False; decision 14 = False; decision 16 = False; decision 18 = False; decision 20 = False; decision 22 = False; decision 23 = False | — |
| 2 | MC/DC | decision 1 = True; decision 2 = True; decision 3 = True; decision 4 = True; decision 5 = True; decision 6 = True | — |
| 3 | Coverage | concrete decision 1 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 4 | MC/DC | decision 10 = True; decision 11 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 5 | MC/DC | decision 11 = False | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 6 | MC/DC | decision 12 = True; decision 13 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 7 | MC/DC | decision 13 = False | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 8 | MC/DC | decision 14 = True; decision 15 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 9 | MC/DC | decision 15 = False | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 10 | MC/DC | decision 16 = True; decision 17 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 11 | MC/DC | decision 17 = False | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 12 | MC/DC | decision 18 = True; decision 19 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 13 | MC/DC | decision 19 = False | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 14 | MC/DC | decision 20 = True; decision 21 = True | — |
| 15 | MC/DC | decision 21 = False | — |
| 16 | MC/DC | decision 22 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 17 | MC/DC | decision 23 = True | PDM_Core0_IndLocalCtrlOpStat.LightIndState[6]=0, PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |

## Infeasible pairs
None.

## Uncontrollable conditions
- decision 7: uncontrollable or degenerate condition
- decision 8: uncontrollable or degenerate condition
- decision 9: uncontrollable or degenerate condition

## Manual actions
- decision 9: intent mismatch
- decision 1: intent mismatch
- decision 20: intent mismatch
- decision 20: intent mismatch
