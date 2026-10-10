' Detached launcher for the scanner-engine supervisor.
'
' WHY THIS EXISTS
' ===============
' A supervisor started as a shell child dies with the shell. Measured twice:
' it stopped logging mid-run and left the scanner down with a stale lock file.
' This engine is meant to run for days and accumulate data, so its lifetime
' cannot depend on a session staying open.
'
' It runs run_supervisor.bat, which holds the real command. Nesting quoted
' executables inside one WScript.Shell.Run line is fragile -- an earlier
' version did that and the supervisor never started.
'
' The supervisor takes the instance lock itself, so double-launching is safe:
' a second copy exits 0 without spawning a child.

Option Explicit

Dim fso, shell, here, bat
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

here = fso.GetParentFolderName(WScript.ScriptFullName)
bat = fso.BuildPath(here, "run_supervisor.bat")

If Not fso.FileExists(bat) Then
    WScript.Echo "not found: " & bat
    WScript.Quit 1
End If

' 0 = hidden window, False = do not wait for it to finish
shell.Run """" & bat & """", 0, False
WScript.Quit 0