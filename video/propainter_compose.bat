@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul
pushd "%~dp0"

rem ==========================================================
rem  propainter_compose.bat
rem  ProPainter が出した連番 PNG を元動画に1パスで合成し、
rem  完成品を1本書き出します。
rem  mask_plan.json をこのバッチにドラッグ＆ドロップしてください。
rem
rem  出力: <元動画>_inpainted.<元の拡張子>
rem        (音声は copy。字幕やチャプターは merge_tracks.bat で戻してください)
rem ==========================================================

set "SCRIPT=%~dp0propainter_tool.py"

rem ---- 設定 ------------------------------------------------
rem WORKDIR を書くと作業フォルダをそこに固定します(空なら JSON の隣)
set "WORKDIR="

rem PATCH=mask : マスク矩形+マージンだけ貼り戻す。crop 外周が無劣化で残る(既定)
rem PATCH=crop : crop 全体を貼り戻す。従来のコマンドと同じ動き
set "PATCH=mask"
set "PATCH_MARGIN=16"

rem overlay の色形式。省くと ffmpeg の既定 yuv420 で全編 8bit 4:2:0 に落ちます。
rem 出力を 8bit 4:2:0 にしたいときだけ yuv420 に変えてください。
set "OVERLAY_FORMAT=yuv422p10"

rem 出力の映像エンコード設定
rem PROFILE 0=Proxy 1=LT 2=422(標準) 3=422HQ 4=4444 5=4444XQ
set "VCODEC=-c:v prores_ks -profile:v 3 -pix_fmt yuv422p10le -vendor apl0"

rem PREFLIGHT=1 で最初の区間境界の前後だけを流して確認します(本番はしません)
set "PREFLIGHT=0"
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

set OPT=--patch %PATCH% --patch-margin %PATCH_MARGIN% --overlay-format %OVERLAY_FORMAT%
if defined WORKDIR set OPT=%OPT% --work-dir "%WORKDIR%"
if defined SOURCE set OPT=%OPT% --source "%SOURCE%"
if "%PREFLIGHT%"=="1" set OPT=%OPT% --preflight

echo.
%PY% "%SCRIPT%" compose --plan "%PLAN%" %OPT% --vcodec "%VCODEC%"

if errorlevel 1 (
    echo.
    echo [ERROR] 合成に失敗しました。上のログを確認してください。
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
