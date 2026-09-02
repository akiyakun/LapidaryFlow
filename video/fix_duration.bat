@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul
pushd "%~dp0"

rem ==========================================================
rem  fix_duration.bat
rem  再生時間(duration)がおかしい動画を、実際の長さに直します。
rem  動画ファイルをこのバッチにドラッグ＆ドロップしてください。
rem  複数ファイルの同時ドロップにも対応しています。
rem
rem  出力: <ファイル名>_fixed.<拡張子>
rem ==========================================================

set "SCRIPT=%~dp0fix_duration.py"

rem ---- 設定 ------------------------------------------------
rem METHOD=remux  : duration を書き直すだけ。無劣化・高速(まずこれ)
rem METHOD=retime : タイムスタンプごと等間隔に振り直す(H.264/H.265 のみ)
set "METHOD=remux"

rem retime のときに使う fps。空なら元ファイルの値を使う (例: 30000/1001)
set "FPS="

rem この秒数以下のズレは正常とみなす
set "TOLERANCE=1.0"

rem KEEPDATA=1 でタイムコード等のデータトラックを残す
rem (既定は破棄。これが誤った duration の原因になっていることが多い)
set "KEEPDATA=0"

rem CHECK=1 で判定のみ(ファイルを作らない)
set "CHECK=0"
rem ----------------------------------------------------------

if not exist "%SCRIPT%" (
    echo fix_duration.py が見つかりません:
    echo   %SCRIPT%
    echo.
    pause
    popd
    exit /b 1
)

if "%~1"=="" (
    echo 動画ファイルをこのバッチファイルにドラッグ＆ドロップしてください。
    echo.
    pause
    popd
    exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg が見つかりません。PATH を確認してください。
    echo.
    pause
    popd
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
    popd
    exit /b 1
)

set "OPT=--method %METHOD% --tolerance %TOLERANCE%"
if defined FPS set "OPT=%OPT% --fps %FPS%"
if "%KEEPDATA%"=="1" set "OPT=%OPT% --keep-data"
if "%CHECK%"=="1" set "OPT=%OPT% --check"

set "FAILED="

:loop
if "%~1"=="" goto done

%PY% "%SCRIPT%" %OPT% "%~f1"

if errorlevel 1 (
    echo.
    echo [ERROR] %~nx1 の処理に失敗しました。
    set "FAILED=1"
)

shift
goto loop

:done
if defined FAILED (
    echo 一部のファイルで問題が残っています。上のログを確認してください。
) else (
    echo すべて完了しました。
)
echo.
pause
popd
