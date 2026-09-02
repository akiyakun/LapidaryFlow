@echo off
chcp 932 >nul

rem ==========================================================
rem  segment_join.bat
rem  Topaz等で処理済みのセグメントが入ったフォルダを
rem  このバッチにドラッグ＆ドロップすると、のりしろを除いて結合します。
rem
rem  segment_plan.json がフォルダ内に無い場合は、
rem  フォルダと segment_plan.json を一緒にドロップするか、
rem  後から表示されるプロンプトにパスを入力してください。
rem ==========================================================

set "SCRIPT=%~dp0segment_tool.py"

rem ---- 設定 ------------------------------------------------
rem COPY_MODE=1 : 再エンコードせずに結合 (ProRes / FFV1 など全イントラ用)
rem COPY_MODE=0 : ENCODE_ARGS でエンコードして結合
set "COPY_MODE=1"
set "ENCODE_ARGS=-c:v libx264 -preset slow -crf 14 -pix_fmt yuv420p"
rem ----------------------------------------------------------

if not exist "%SCRIPT%" (
    echo segment_tool.py が見つかりません:
    echo   %SCRIPT%
    echo.
    pause
    exit /b 1
)

if "%~1"=="" (
    echo 処理済みセグメントが入ったフォルダを
    echo このバッチファイルにドラッグ＆ドロップしてください。
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

rem ---- ドロップされた項目を仕分け --------------------------
set "PROCDIR="
set "MANIFEST="

:parse
if "%~1"=="" goto parsed
if exist "%~1\" (
    set "PROCDIR=%~f1"
) else (
    if /i "%~x1"==".json" set "MANIFEST=%~f1"
)
shift
goto parse
:parsed

if not defined PROCDIR (
    echo フォルダがドロップされていません。
    echo 処理済みセグメントが入ったフォルダをドロップしてください。
    echo.
    pause
    exit /b 1
)

if "%PROCDIR:~-1%"=="\" set "PROCDIR=%PROCDIR:~0,-1%"

if not defined MANIFEST (
    if exist "%PROCDIR%\segment_plan.json" set "MANIFEST=%PROCDIR%\segment_plan.json"
)

rem set /p の結果を同じブロック内で参照すると展開されないため goto で分岐する
if defined MANIFEST goto have_manifest
echo segment_plan.json が見つかりませんでした。
set /p "MANIFEST=パスを入力するかファイルをここにドラッグしてEnter: "
set "MANIFEST=%MANIFEST:"=%"
:have_manifest

if not exist "%MANIFEST%" (
    echo segment_plan.json が見つかりません:
    echo   %MANIFEST%
    echo.
    pause
    exit /b 1
)

rem ---- 出力ファイル名を決定 --------------------------------
set "EXT="
if "%COPY_MODE%"=="1" (
    for %%F in ("%PROCDIR%\*.mov" "%PROCDIR%\*.mkv" "%PROCDIR%\*.mp4" "%PROCDIR%\*.avi") do (
        if not defined EXT set "EXT=%%~xF"
    )
) else (
    set "EXT=.mp4"
)

if not defined EXT (
    echo フォルダ内に動画ファイルが見つかりません:
    echo   %PROCDIR%
    echo.
    pause
    exit /b 1
)

for %%D in ("%PROCDIR%") do set "PDNAME=%%~nxD"
for %%D in ("%PROCDIR%") do set "PDPARENT=%%~dpD"
set "OUTPUT=%PDPARENT%%PDNAME%_joined%EXT%"

echo ==========================================================
echo  manifest : %MANIFEST%
echo  segments : %PROCDIR%
echo  output   : %OUTPUT%
echo ==========================================================
echo.

if "%COPY_MODE%"=="1" (
    %PY% "%SCRIPT%" join --manifest "%MANIFEST%" --processed-dir "%PROCDIR%" --output "%OUTPUT%" --copy
) else (
    %PY% "%SCRIPT%" join --manifest "%MANIFEST%" --processed-dir "%PROCDIR%" --output "%OUTPUT%" --encode-args "%ENCODE_ARGS%"
)

if errorlevel 1 (
    echo.
    echo [ERROR] 結合に失敗しました。上のログを確認してください。
) else (
    echo.
    echo 完了しました: %OUTPUT%
)

echo.
pause
