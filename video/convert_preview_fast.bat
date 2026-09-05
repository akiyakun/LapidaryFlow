@echo off
chcp 65001 >nul
setlocal

rem ==========================================================
rem convert_preview_fast_preserve.bat
rem
rem 編集途中の一時確認 / ProRes・FFV1非対応ツール向け MP4。
rem 速度優先で変換しつつ、元動画の
rem   ・時間長
rem   ・フレームレート
rem   ・解像度
rem を維持します。
rem
rem 動画ファイルをこのBATへドラッグ＆ドロップしてください。
rem 複数ファイルの同時ドロップにも対応します。
rem ==========================================================

if "%~1"=="" (
    echo 動画ファイルをこのBATへドラッグ＆ドロップしてください。
    echo.
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo Python が見つかりません。PATH を確認してください。
    echo.
    pause
    exit /b 1
)

python "%~dp0convert_preview_fast_preserve.py" %*

set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo エラーが発生しました。
)
pause
exit /b %RC%
