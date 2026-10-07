Option Explicit
' Guarded launcher for taopiaopiao monitor.
' Runs the monitor in a hidden console; when it exits, waits 5 seconds
' and relaunches it, forever, unless stop-flag file exists.
Dim fso, sh, here, py, args, stopFlag
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
py = here & "\.venv\Scripts\python.exe"
stopFlag = here & "\.stop-monitor"

' do not inherit a dead desktop proxy, child may otherwise get 10061
sh.Environment("PROCESS")("HTTP_PROXY")  = ""
sh.Environment("PROCESS")("HTTPS_PROXY") = ""
sh.Environment("PROCESS")("http_proxy")  = ""
sh.Environment("PROCESS")("https_proxy") = ""
sh.Environment("PROCESS")("ALL_PROXY")   = ""
sh.Environment("PROCESS")("all_proxy")   = ""

sh.CurrentDirectory = here
args = "cmd /c """"" & py & """ -u -m monitor 1>>""" & here & "\monitor.out.log"" 2>>""" & here & "\monitor.err.log"""""

Do While Not fso.FileExists(stopFlag)
  sh.Run args, 0, True   ' wait until the child exits, then loop
  If fso.FileExists(stopFlag) Then Exit Do
  WScript.Sleep 5000     ' give the port TIME_WAIT some slack before relaunch
Loop

' clean the flag so the next start.bat start works
On Error Resume Next
fso.DeleteFile stopFlag, True
