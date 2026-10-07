@echo off
setlocal EnableExtensions EnableDelayedExpansion
title Taopiaopiao Monitor Launcher

set "HERE=%~dp0"
cd /d "%HERE%"

set "ACTION=%~1"
if "%ACTION%"=="" set "ACTION=start"
if /i "%ACTION%"=="start"   goto action_ok
if /i "%ACTION%"=="stop"    goto action_ok
if /i "%ACTION%"=="restart" goto action_ok
if /i "%ACTION%"=="status"  goto action_ok
echo Usage: start.bat [start^|stop^|restart^|status]
exit /b 1

:action_ok
rem ============ 1) find a base python ============
set "BASEPY="
python --version >nul 2>nul
if not errorlevel 1 set "BASEPY=python"
if defined BASEPY goto base_done
py --version >nul 2>nul
if not errorlevel 1 set "BASEPY=py"
if defined BASEPY goto base_done
set "CAND=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if exist "%CAND%" set "BASEPY=%CAND%"
:base_done
if not defined BASEPY goto no_python

rem ============ 2) ensure .venv exists ============
set "VENV_PY=%HERE%.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto venv_done
echo [setup] creating .venv with %BASEPY% ...
"%BASEPY%" -m venv "%HERE%.venv"
if errorlevel 1 goto venv_fail
echo [setup] installing requirements ...
"%VENV_PY%" -m pip install -q -r requirements.txt
if errorlevel 1 goto deps_fail
:venv_done

rem ============ 3) read port from config.yaml ============
set "PORT=8787"
"%VENV_PY%" -c "import yaml;print(yaml.safe_load(open('config.yaml',encoding='utf-8'))['listen_port'])" > "%TEMP%\tpp_port.txt" 2>nul
set /p CFG_PORT=<"%TEMP%\tpp_port.txt"
del "%TEMP%\tpp_port.txt" >nul 2>nul
if not "%CFG_PORT%"=="" set "PORT=%CFG_PORT%"

rem ============ dispatch ============
if /i "%ACTION%"=="stop"    goto do_stop
if /i "%ACTION%"=="restart" goto do_restart
if /i "%ACTION%"=="status"  goto do_status
goto do_start

:do_stop
rem ---- create stop flag FIRST so the guard loop does not relaunch ----
type nul > "%HERE%.stop-monitor"
rem ---- kill monitor process AND the guard (wscript) itself ----
"%VENV_PY%" "%HERE%stop_port.py" --guards %PORT%
echo [stop] done. port %PORT% released.
exit /b 0

:do_status
"%VENV_PY%" -c "import urllib.request as u;u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%PORT%/',timeout=3)" >nul 2>nul
if errorlevel 1 (
    echo [status] NOT RUNNING on port %PORT%
    exit /b 1
)
echo [status] RUNNING on port %PORT%
exit /b 0

:do_start
if not exist "%HERE%config.yaml" goto no_config
if not exist "%HERE%monitor\__main__.py" goto no_files

rem ---- clear stale stop flag so the guard can relaunch ----
if exist "%HERE%.stop-monitor" del "%HERE%.stop-monitor" >nul 2>nul

"%VENV_PY%" -c "import urllib.request as u;u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%PORT%/',timeout=3)" >nul 2>nul
if not errorlevel 1 (
    echo [start] already running on port %PORT%, nothing to do.
    exit /b 0
)

rem ---- clean proxy env ----
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "http_proxy="
set "https_proxy="
set "ALL_PROXY="
set "all_proxy="

rem ---- free the port, wait for TIME_WAIT ----
"%VENV_PY%" "%HERE%stop_port.py" %PORT%
ping -n 3 127.0.0.1 >nul

rem ---- launch hidden via wscript (survives console close) ----
set "HAS_WSCRIPT=0"
where wscript.exe >nul 2>nul
if not errorlevel 1 set "HAS_WSCRIPT=1"

if "%HAS_WSCRIPT%"=="1" goto launch_wscript
echo [start] wscript not available, using visible console window.
start "tpp-monitor" /min /D "%HERE%" "%VENV_PY%" -u -m monitor
goto wait_up

:launch_wscript
wscript //nologo "%HERE%start-hidden.vbs"
goto wait_up

:wait_up
set /a TRY=0
:wait_loop
set /a TRY+=1
"%VENV_PY%" -c "import urllib.request as u;u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:%PORT%/',timeout=3)" >nul 2>nul
if not errorlevel 1 goto up_ok
if %TRY% GEQ 30 goto start_fail
ping -n 2 127.0.0.1 >nul
goto wait_loop

:up_ok
echo [start] OK. web UI: http://127.0.0.1:%PORT%/
"%VENV_PY%" -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print('[start] LAN address: http://'+s.getsockname()[0]+':%PORT%/')" 2>nul

rem ---- open browser so double-click users see the UI right away ----
rem (skipped when invoked with NOBROWSER, e.g. from automation)
if /i not "%NOBROWSER%"=="1" start "" "http://127.0.0.1:%PORT%/"
exit /b 0

:do_restart
rem ---- stop (with guard flag, kill old guard) then start again ----
type nul > "%HERE%.stop-monitor"
"%VENV_PY%" "%HERE%stop_port.py" --guards %PORT%
ping -n 3 127.0.0.1 >nul
goto do_start

rem ============ error exits ============
rem failures keep the window open so double-click users can read the message
:no_config
echo [error] config.yaml not found. copy config.example.yaml to config.yaml first.
pause
exit /b 1

:no_files
echo [error] monitor\__main__.py not found. run this bat from the project root.
pause
exit /b 1

:no_python
echo [error] no python found on PATH, py launcher, or workbuddy binaries.
pause
exit /b 1

:venv_fail
echo [error] failed to create .venv with %BASEPY%.
pause
exit /b 1

:deps_fail
echo [error] pip install failed. check network / proxy settings.
pause
exit /b 1

:start_fail
echo [start] FAILED: service did not come up after 30 tries. check monitor.err.log
echo --- last lines of monitor.err.log ---
type "%HERE%monitor.err.log" 2>nul | more +0
pause
exit /b 1
