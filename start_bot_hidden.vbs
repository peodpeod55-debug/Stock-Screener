' Launch run_bot.bat with no console window (used by the Startup shortcut)
Set fso = CreateObject("Scripting.FileSystemObject")
projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = projectDir
sh.Run """" & projectDir & "\run_bot.bat""", 0, False
