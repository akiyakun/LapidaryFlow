@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul

rem ==========================================================
rem  convert_prores.bat
rem  動画ファイルをこのバッチにドラッグ＆ドロップすると
rem  同じ場所に <ファイル名>_prores.mov を作ります。
rem  複数ファイルの同時ドロップにも対応しています。
rem ==========================================================

rem ---- 設定 ------------------------------------------------
rem PROFILE 0=Proxy 1=LT 2=422(標準) 3=422HQ 4=4444 5=4444XQ
set "PROFILE=3"
set "PIXFMT=yuv422p10le"
set "AUDIO=-c:a pcm_s16le"
rem ----------------------------------------------------------

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg が見つかりません。PATH を確認してください。
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

if "%PROFILE%"=="4" set "PIXFMT=yuva444p10le"
if "%PROFILE%"=="5" set "PIXFMT=yuva444p10le"

set "FAILED="

:loop
if "%~1"=="" goto done

set "OUT=%~dp1%~n1_prores.mov"

echo ==========================================================
echo  %~nx1  ^-^>  %~n1_prores.mov
echo ==========================================================

if exist "%OUT%" (
    echo [SKIP] 出力先が既に存在します: %OUT%
    goto next
)

ffmpeg -hide_banner -loglevel warning -stats -i "%~f1" ^
    -map 0:v -map 0:a? ^
    -c:v prores_ks -profile:v %PROFILE% -pix_fmt %PIXFMT% -vendor apl0 ^
    %AUDIO% ^
    "%OUT%"

if errorlevel 1 (
    echo.
    echo [ERROR] %~nx1 の変換に失敗しました。
    set "FAILED=1"
)

:next
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
