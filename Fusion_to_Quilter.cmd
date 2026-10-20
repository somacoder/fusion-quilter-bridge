@echo off
rem Fusion 360 EAGLE export (input\) -> KiCad project for Quilter (to_quilter\).  Extra arguments are passed on,
rem e.g.  Fusion_to_Quilter.cmd --name My_Board
setlocal
set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\KiCad\9.0\bin\python.exe" "%ProgramFiles%\KiCad\9.0\bin\python.exe") do (
  if not defined PY if exist %%P set "PY=%%~P"
)
if not defined PY (
  echo KiCad 9 was not found. Install it from https://www.kicad.org/download/windows/
  set RC=1
  goto :end
)
"%PY%" -u "%~dp0tools\fusion_to_quilter.py" %*
set RC=%ERRORLEVEL%
:end
if not defined QUILTER_NOPAUSE echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause
exit /b %RC%
