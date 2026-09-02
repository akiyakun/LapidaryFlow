@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul

rem ==========================================================
rem  convert_ffv1.bat
rem  動画ファイルをこのバッチにドラッグ＆ドロップすると
rem  同じ場所に <ファイル名>_ffv1.mkv を作ります。
rem  FFV1 は可逆圧縮なので映像は元データのまま保持されます。
rem  複数ファイルの同時ドロップにも対応しています。
rem ==========================================================

rem ---- 設定 ------------------------------------------------
rem SLICES を増やすとマルチスレッドが効くが僅かにサイズが増える
set "SLICES=24"
rem PIXFMT を空にすると入力のピクセルフォーマットをそのまま使う
set "PIXFMT="
set "AUDIO=-c:a copy"
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

set "PIXOPT="
if defined PIXFMT set "PIXOPT=-pix_fmt %PIXFMT%"

set "FAILED="

:loop
if "%~1"=="" goto done

set "OUT=%~dp1%~n1_ffv1.mkv"

echo ==========================================================
echo  %~nx1  ^-^>  %~n1_ffv1.mkv
echo ==========================================================

if exist "%OUT%" (
    echo [SKIP] 出力先が既に存在します: %OUT%
    goto next
)

rem -g 1 で全フレームをキーフレームにし、segment_join の --copy 結合に対応させる
ffmpeg -hide_banner -loglevel warning -stats -i "%~f1" ^
    -map 0:v -map 0:a? ^
    -c:v ffv1 -level 3 -coder 1 -context 1 -g 1 -slices %SLICES% -slicecrc 1 %PIXOPT% ^
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
