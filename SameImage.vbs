Option Explicit
Dim shell, files, folder, python, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = folder
python = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")
If Not files.FileExists(python) Then
    python = "pythonw.exe"
End If
command = Chr(34) & python & Chr(34) & " " & Chr(34) & files.BuildPath(folder, "app.py") & Chr(34)
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    MsgBox "Cannot start SameImage. Please run setup.bat first." & vbCrLf & Err.Description, 16, "SameImage"
End If
