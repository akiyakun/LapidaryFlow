@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul
pushd "%~dp0"

rem ==========================================================
rem  frame_count.bat
rem  ドロップされた動画のフレーム数をテキストファイルに書き出します。
rem  動画ファイルをこのバッチにドラッグ＆ドロップしてください。
rem  複数ファイルの同時ドロップにも対応しています。
rem
rem  出力: 最初の動画と同じフォルダの frame_count.txt
rem        (タブ区切りなので表計算ソフトにそのまま貼れます)
rem ==========================================================

set "SCRIPT=%~dp0frame_count.py"

rem ---- 設定 ------------------------------------------------
rem METHOD=packets : パケット数を数える。デコードしないので高速(まずこれ)
rem METHOD=decode  : 実際にデコードして数える。低速だが確実
rem METHOD=meta    : コンテナのメタデータを読むだけ。一瞬だが値が無い/嘘のことがある
set "METHOD=packets"

rem APPEND=1 で frame_count.txt に追記する(既定は上書き)
set "APPEND=0"

rem FULLPATH=1 でファイル名をフルパスで書く
set "FULLPATH=0"
rem ----------------------------------------------------------

if not exist "%SCRIPT%" (
    echo frame_count.py が見つかりません:
    echo   %SCRIPT%
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

rem ---- ドロップされたファイルを集める ----------------------
set "FILES="

:loop
if "%~1"=="" goto loop_end
if exist "%~1\" goto next
set FILES=%FILES% "%~f1"
:next
shift
goto loop
:loop_end

rem set /p の結果は同じブロック内では展開されないため goto で分岐する
if defined FILES goto have
echo 動画ファイルをこの画面にドラッグしてEnterを押してください。
set "ONE="
set /p "ONE=  動画: "
if not defined ONE goto noinput
set "ONE=%ONE:"=%"
:trim
if not defined ONE goto noinput
if "%ONE:~0,1%"==" " set "ONE=%ONE:~1%" & goto trim
if "%ONE:~-1%"==" " set "ONE=%ONE:~0,-1%" & goto trim
set FILES="%ONE%"
:have

set "OPT=--method %METHOD%"
if "%APPEND%"=="1" set "OPT=%OPT% --append"
if "%FULLPATH%"=="1" set "OPT=%OPT% --full-path"

echo.
%PY% "%SCRIPT%" %OPT% %FILES%

if errorlevel 1 (
    echo.
    echo [ERROR] 一部のファイルでフレーム数を取得できませんでした。
    goto fail
)

echo.
pause
popd
exit /b 0

:noinput
echo 動画が指定されていません。
:fail
echo.
pause
popd
exit /b 1
