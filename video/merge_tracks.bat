@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul
pushd "%~dp0"

rem ==========================================================
rem  merge_tracks.bat
rem  加工後の動画(映像のみ)に、加工前の元動画の音声・字幕などを戻します。
rem
rem    A = 分割前の元動画   (音声・字幕・チャプターの供給元)
rem    B = segment_join 後の加工済み動画 (映像の供給元)
rem
rem  使い方はどちらでもOK:
rem    ・2つの動画を一緒にこのバッチにドラッグ＆ドロップ
rem    ・1つだけドロップ(または直接実行)して、残りをこの画面にドロップ＋Enter
rem
rem  どちらがどちらかは中身を見て自動判定するので、順番は問いません。
rem  すべて無劣化コピー(再エンコードなし)です。
rem
rem  出力: <加工済み動画名>_merged.<拡張子>
rem ==========================================================

set "SCRIPT=%~dp0merge_tracks.py"

rem ---- 設定 ------------------------------------------------
rem CHAPTERS=1 : 元動画のチャプターを引き継ぐ
rem CHAPTERS=0 : チャプターを捨てる(尺がおかしくなる場合はこちら)
set "CHAPTERS=1"

rem DATA=1 でタイムコード等のデータストリームも引き継ぐ
set "DATA=0"

rem OVERWRITE=1 で出力先を上書きする
set "OVERWRITE=0"
rem ----------------------------------------------------------

if not exist "%SCRIPT%" (
    echo merge_tracks.py が見つかりません:
    echo   %SCRIPT%
    goto fail
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg が見つかりません。PATH を確認してください。
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

rem ---- ドロップされたファイルを仕分け ----------------------
set "FILE1="
set "FILE2="
set "EXTRA="

:parse
if "%~1"=="" goto parsed
if exist "%~1\" goto next
if not defined FILE1 set "FILE1=%~f1" & goto next
if not defined FILE2 set "FILE2=%~f1" & goto next
set "EXTRA=1"
:next
shift
goto parse
:parsed

if defined EXTRA (
    echo 動画ファイルは2つまでです。
    goto fail
)

echo ==========================================================
echo  加工後の動画(映像)と、分割前の元動画(音声・字幕)を結合します。
echo ==========================================================
echo.

rem set /p の結果は同じブロック内では展開されないため goto で分岐する
if defined FILE1 goto have1
echo 1つ目の動画をこの画面にドラッグしてEnterを押してください。
set /p "FILE1=  1つ目: "
set "FILE1=%FILE1:"=%"
:have1

if defined FILE2 goto have2
echo.
echo もう1つの動画をこの画面にドラッグしてEnterを押してください。
echo (加工後の動画と分割前の元動画の2つが揃えばOK。順番は問いません)
set /p "FILE2=  2つ目: "
set "FILE2=%FILE2:"=%"
:have2

if not defined FILE1 goto noinput
if not defined FILE2 goto noinput

if not exist "%FILE1%" (
    echo ファイルが見つかりません:
    echo   %FILE1%
    goto fail
)
if not exist "%FILE2%" (
    echo ファイルが見つかりません:
    echo   %FILE2%
    goto fail
)

set "OPT="
if "%CHAPTERS%"=="0" set "OPT=%OPT% --no-chapters"
if "%DATA%"=="1" set "OPT=%OPT% --include-data"
if "%OVERWRITE%"=="1" set "OPT=%OPT% --overwrite"

echo.
%PY% "%SCRIPT%"%OPT% "%FILE1%" "%FILE2%"

if errorlevel 1 (
    echo.
    echo [ERROR] 結合に失敗しました。上のログを確認してください。
    echo 自動判定に失敗した場合は、コマンドラインから
    echo   merge_tracks.py --video "映像を使う動画" --tracks "音声を使う動画"
    echo のように役割を指定してください。
) else (
    echo.
    echo 完了しました。
)

echo.
pause
popd
exit /b 0

:noinput
echo 動画が2つ指定されていません。
:fail
echo.
pause
popd
exit /b 1
