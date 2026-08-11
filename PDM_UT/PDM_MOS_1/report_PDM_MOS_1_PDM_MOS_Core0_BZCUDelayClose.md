# UTGen report: PDM_MOS_Core0_BZCUDelayClose

- Function: `PDM_MOS_Core0_BZCUDelayClose`
- File: ``
- Test cases: 9
- Coverage: 4 decisions; MC/DC=9

| id | kind | description | expected outputs |
|---:|---|---|---|
| 1 | MC/DC | decision 1 = False | PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 2 | MC/DC | decision 1 = True | PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 3 | MC/DC | decision 1 = True | PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 4 | MC/DC | decision 2 = True; decision 3 = True | PDM_Core0_LoadStat.LoadCtrl[13]=1, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 5 | MC/DC | decision 2 = False; decision 4 = True | PDM_Core0_LoadStat.LoadCtrl[13]=1, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 6 | MC/DC | decision 2 = False | PDM_Core0_LoadStat.LoadCtrl[13]=1, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 7 | MC/DC | decision 2 = False | PDM_Core0_LoadStat.LoadCtrl[13]=0, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 8 | MC/DC | decision 3 = False | PDM_Core0_LoadStat.LoadCtrl[13]=1, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |
| 9 | MC/DC | decision 4 = False | PDM_Core0_LoadStat.LoadCtrl[13]=1, PDM_Core0_LoadStat.LoadCtrlSource[13]=0 |

## Infeasible pairs
None.

## Uncontrollable conditions
None.

## Manual actions
None.
