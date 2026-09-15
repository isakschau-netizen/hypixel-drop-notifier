Set sh = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = folder
' Use "python" if it is on PATH, otherwise the "py" launcher that the python.org
' installer adds by default. (The Microsoft Store stub for "python" fails --version.)
py = "python"
If sh.Run("cmd /c python --version", 0, True) <> 0 Then py = "py -3"
' 0 = hidden window, False = don't wait. Output goes to notifier.log.
' PYTHONIOENCODING=utf-8 so the redirected log can hold item glyphs (the code
' also forces UTF-8 itself; this is just belt-and-suspenders).
sh.Run "cmd /c set PYTHONIOENCODING=utf-8&& " & py & " hypixel_drop_notifier.py >> notifier.log 2>&1", 0, False
