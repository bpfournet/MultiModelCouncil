@echo off
setlocal EnableExtensions DisableDelayedExpansion
title Stop MultiModelCouncil Server
rem Share the launcher's exact process identification; never kill a process tree.
call "%~dp0Launch-MAF.cmd" --stop
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
