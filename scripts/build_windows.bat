@echo off
setlocal
cd /d "%~dp0"

set "MSYS_ROOT=%~1"
if "%MSYS_ROOT%"=="" set "MSYS_ROOT=C:\msys64"
set "JOBS=%~2"
if "%JOBS%"=="" set "JOBS=4"
set "MSYS_BIN=%MSYS_ROOT%\usr\bin"
set "BASH=%MSYS_BIN%\bash.exe"
set "CYGPATH=%MSYS_BIN%\cygpath.exe"
set "GEM5=%~dp0..\gem5"
set "TOOLS=%~dp0..\.tools"
set "BUILD_TARGET=build/Garnet_standalone/gem5.opt.exe"
if "%~3"=="--stats-test" set "BUILD_TARGET=build/Garnet_standalone/base/stats/storage.test.opt.exe"
if "%~3"=="--ruby-set-test" set "BUILD_TARGET=build/Garnet_standalone/mem/ruby/common/set.test.opt.exe"
if "%~3"=="--stats-info-test" set "BUILD_TARGET=build/Garnet_standalone/base/stats/info.test.opt.exe"
if "%~3"=="--drain-test" set "BUILD_TARGET=build/Garnet_standalone/sim/drain.test.opt.exe"
if "%~3"=="--requestor-id-test" set "BUILD_TARGET=build/Garnet_standalone/mem/requestor_id.test.opt.exe"
if not "%~3"=="" if not "%~3"=="--stats-test" if not "%~3"=="--ruby-set-test" if not "%~3"=="--stats-info-test" if not "%~3"=="--drain-test" if not "%~3"=="--requestor-id-test" (
  echo ERROR: Third argument must be --stats-test, --ruby-set-test, --stats-info-test, --drain-test, --requestor-id-test or omitted.
  exit /b 1
)

if not exist "%BASH%" (
  echo ERROR: MSYS2 bash not found: %BASH%
  exit /b 1
)
if not exist "%GEM5%\SConstruct" (
  echo ERROR: gem5 source is missing: %GEM5%
  echo Run install_windows.ps1 once to fetch and patch gem5.
  exit /b 1
)

rem Network.py is the canonical Omelet runtime config, copied on Windows.
copy /Y "%~dp0..\omelet\backends\gem5\configs\network\Network.py" "%GEM5%\configs\network\Network.py" >nul
if errorlevel 1 exit /b 1
copy /Y "%~dp0..\omelet\backends\gem5\configs\network\native_graph.py" "%GEM5%\configs\network\native_graph.py" >nul
if errorlevel 1 exit /b 1
copy /Y "%~dp0..\omelet\backends\gem5\configs\ruby\Garnet_standalone.py" "%GEM5%\configs\ruby\Garnet_standalone.py" >nul
if errorlevel 1 exit /b 1

for /f "delims=" %%I in ('%CYGPATH% -u "%GEM5%"') do set "GEM5_UNIX=%%I"
for /f "delims=" %%I in ('%CYGPATH% -u "%TOOLS%"') do set "TOOLS_UNIX=%%I"
if not defined GEM5_UNIX exit /b 1
if not defined TOOLS_UNIX exit /b 1

echo Building Omelet gem5 incrementally with MSYS2 SCons...
set "PATH=%MSYS_BIN%;%PATH%"
rem MSYSTEM=MSYS matches install_windows.ps1:65 so gcc sees /usr/include
rem (Python.h, sys/select.h) instead of only the mingw64 sysroot.
rem This only affects the build shell; it does not change gem5 behavior.
set "MSYSTEM=MSYS"
"%BASH%" -lc "cd '%GEM5_UNIX%' && PYTHONPATH='%TOOLS_UNIX%' /usr/bin/python3 -c 'import SCons.Script; SCons.Script.main()' --ignore-style --no-compress-debug %BUILD_TARGET% -j%JOBS%"
if errorlevel 1 exit /b 1

rem Remove DWARF build symbols; gem5 trace flags and simulation are unchanged.
"%BASH%" -lc "cd '%GEM5_UNIX%' && /usr/bin/strip --strip-debug %BUILD_TARGET%"
if errorlevel 1 exit /b 1
if not "%~3"=="" (
  "%BASH%" -lc "cd '%GEM5_UNIX%' && ./%BUILD_TARGET%"
  if errorlevel 1 exit /b 1
)
echo Omelet build ready: %GEM5%\%BUILD_TARGET%
endlocal
