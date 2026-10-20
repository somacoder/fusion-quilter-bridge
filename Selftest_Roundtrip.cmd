@echo off
rem Round-trip self-test on your own design, without Quilter (run after Fusion_to_Quilter).  Works in a scratch
rem folder; log: to_quilter\<name>\logs\selftest.log.  Extra arguments are passed on, e.g.  --name My_Board  --keep
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
"%PY%" -u "%~dp0tools\selftest_roundtrip.py" %*
set RC=%ERRORLEVEL%
:end
if not defined QUILTER_NOPAUSE echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause
exit /b %RC%
