Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
Set env = sh.Environment("PROCESS")
' Force Python into UTF-8 mode so emoji prints can never crash it.
env("PYTHONUTF8") = "1"
env("PYTHONIOENCODING") = "utf-8"
proj = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = proj
q = Chr(34)

py = fso.BuildPath(proj, ".venv\Scripts\python.exe")
If Not fso.FileExists(py) Then py = "python.exe"

logf = proj & "\ira_startup.log"
' Run hidden; capture fresh output for verification.
cmd = "cmd /c " & q & q & py & q & " " & q & proj & "\core\main.py" & q & " > " & q & logf & q & " 2>&1" & q
sh.Run cmd, 0, False
