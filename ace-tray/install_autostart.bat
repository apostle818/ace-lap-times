@echo off
echo Creating startup shortcut for ACE Lap Tracker...
echo.

:: Get the current directory
set "APP_DIR=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\ACE Lap Tracker.lnk"

:: Create a VBS script to make the shortcut (Windows doesn't have native shortcut creation)
set "VBS_TEMP=%TEMP%\create_ace_shortcut.vbs"

echo Set oWS = WScript.CreateObject("WScript.Shell") > "%VBS_TEMP%"
echo Set oLink = oWS.CreateShortcut("%SHORTCUT%") >> "%VBS_TEMP%"
echo oLink.TargetPath = "%APP_DIR%.venv\Scripts\pythonw.exe" >> "%VBS_TEMP%"
echo oLink.Arguments = "ace_tray.py" >> "%VBS_TEMP%"
echo oLink.WorkingDirectory = "%APP_DIR%" >> "%VBS_TEMP%"
echo oLink.Description = "ACE Lap Tracker Tray App" >> "%VBS_TEMP%"
echo oLink.Save >> "%VBS_TEMP%"

cscript //nologo "%VBS_TEMP%"
del "%VBS_TEMP%"

echo.
echo Done! ACE Lap Tracker will now start automatically when you log in.
echo Shortcut created at: %SHORTCUT%
echo.
echo To remove auto-start, delete the shortcut from:
echo   %STARTUP%
echo.
pause
