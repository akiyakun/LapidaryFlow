@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul
pushd "%~dp0"

rem ==========================================================
rem  propainter_extract.bat
rem  マスクツールが書き出した mask_plan.json から、
rem  パターンごとのマスク PNG と区間ごとの切り出し連番 PNG を作ります。
rem  mask_plan.json をこのバッチにドラッグ＆ドロップしてください。
rem  元動画が JSON に書いた場所に無いときは、
rem  JSON と元動画を一緒にドロップすると動画の方が優先されます。
rem
rem  出力: JSON と同じフォルダの <JSON名>_work\
rem          masks\<パターン名>_crop_mask.png
rem          frames\<区間名>\00000000.png ...
rem ==========================================================

set "SCRIPT=%~dp0propainter_tool.py"

rem ---- 設定 ------------------------------------------------
rem WORKDIR を書くと作業フォルダをそこに固定します(空なら JSON の隣)
set "WORKDIR="

rem FORCE=1 で既に連番 PNG があっても作り直します
set "FORCE=0"
rem ----------------------------------------------------------

if not exist "%SCRIPT%" (
    echo propainter_tool.py が見つかりません:
    echo   %SCRIPT%
    goto fail
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg が見つかりません。PATH を確認してください。
    goto fail
)

where ffprobe >nul 2>&1
if errorlevel 1 (
    echo ffprobe が見つかりません。PATH を確認してください。
    goto fail
)

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo Python が見つかりません。PATH を確認してください。
    goto fail
)

rem ---- ドロップされた項目を仕分け --------------------------
set "PLAN="
set "SOURCE="

:parse
if "%~1"=="" goto parsed
if exist "%~1\" goto next
if /i "%~x1"==".json" (
    set "PLAN=%~f1"
) else (
    set "SOURCE=%~f1"
)
:next
shift
goto parse
:parsed

rem set /p の結果は同じブロック内では展開されないため goto で分岐する
if defined PLAN goto have
echo mask_plan.json をこの画面にドラッグしてEnterを押してください。
set "ONE="
set /p "ONE=  JSON: "
if not defined ONE goto noinput
set "ONE=%ONE:"=%"
:trim
if not defined ONE goto noinput
if "%ONE:~0,1%"==" " set "ONE=%ONE:~1%" & goto trim
if "%ONE:~-1%"==" " set "ONE=%ONE:~0,-1%" & goto trim
set "PLAN=%ONE%"
:have

if not exist "%PLAN%" (
    echo JSON が見つかりません: %PLAN%
    goto fail
)

set OPT=
if defined WORKDIR set OPT=%OPT% --work-dir "%WORKDIR%"
if defined SOURCE set OPT=%OPT% --source "%SOURCE%"
if "%FORCE%"=="1" set OPT=%OPT% --force

echo.
%PY% "%SCRIPT%" extract --plan "%PLAN%" %OPT%

if errorlevel 1 (
    echo.
    echo [ERROR] 切り出しに失敗しました。上のログを確認してください。
    goto fail
)

echo.
pause
popd
exit /b 0

:noinput
echo JSON が指定されていません。
:fail
echo.
pause
popd
exit /b 1
