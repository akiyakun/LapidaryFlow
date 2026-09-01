@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul

rem ==========================================================
rem  segment_cut.bat
rem  動画ファイルをこのバッチにドラッグ＆ドロップすると
rem  同じ場所に <ファイル名>_cut フォルダを作って分割します。
rem  複数ファイルの同時ドロップにも対応しています。
rem ==========================================================

set "SCRIPT=%~dp0segment_tool.py"

rem ---- 設定 ------------------------------------------------
set "CHUNK=15:00"
set "HANDLE=5"
set "ALIGN=scene"
rem ----------------------------------------------------------

if not exist "%SCRIPT%" (
    echo segment_tool.py が見つかりません:
    echo   %SCRIPT%
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo 動画ファイルをこのバッチファイルにドラッグ＆ドロップしてください。
    echo.
    pause
    exit /b 1
)

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Python が見つかりません。PATH を確認してください。
    echo.
    pause
    exit /b 1
)

set "FAILED="

:loop
if "%~1"=="" goto done

echo ==========================================================
echo  %~nx1
echo ==========================================================

%PY% "%SCRIPT%" cut --source "%~f1" --output-dir "%~dp1%~n1_cut" --chunk %CHUNK% --handle %HANDLE% --align %ALIGN%

if errorlevel 1 (
    echo.
    echo [ERROR] %~nx1 の処理に失敗しました。
    set "FAILED=1"
)

echo.
shift
goto loop

:done
if defined FAILED (
    echo 一部のファイルでエラーが発生しました。上のログを確認してください。
) else (
    echo すべて完了しました。
)
echo.
pause
