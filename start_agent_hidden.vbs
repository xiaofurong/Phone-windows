' Run run.bat (daemon PC Control Agent) hidden, no console window.
Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """C:\Users\zhw19\pc-control\run.bat""", 0, False
