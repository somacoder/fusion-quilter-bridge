@echo off
rem Quilter result (newest .kicad_pcb / .zip in from_quilter\) -> Fusion 360 script + board (to_fusion\).
rem Extra arguments are passed on, e.g.  Quilter_to_Fusion.cmd from_quilter\candidate_2.kicad_pcb
setlocal
set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\KiCad\9.0\bin\python.exe" "%ProgramFiles%\KiCad\9.0\bin\python.exe" "%LOCALAPPDATA%\Programs\KiCad\10.0\bin\python.exe" "%ProgramFiles%\KiCad\10.0\bin\python.exe") do (
  if not defined PY if exist %%P set "PY=%%~P"
)
if not defined PY (
  echo No Python found. Install KiCad 9 from https://www.kicad.org/download/windows/ ^(it includes Python^)
  set RC=1
  goto :end
)
"%PY%" -u "%~dp0tools\quilter_to_fusion.py" %*
set RC=%ERRORLEVEL%
:end
if not defined QUILTER_NOPAUSE echo %CMDCMDLINE% | find /i "%~nx0" >nul && pause
exit /b %RC%
