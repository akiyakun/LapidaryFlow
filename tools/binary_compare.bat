@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul
setlocal enabledelayedexpansion

rem ==========================================================
rem  binary_compare.bat
rem  2つのファイル (または2つのフォルダ) をこのバッチに
rem  ドラッグ＆ドロップすると、バイナリが完全一致するか調べます。
rem  フォルダをドロップした場合はサブフォルダも含めて
rem  同じ相対パスのファイル同士を比較します。
rem ==========================================================

if "%~2"=="" goto usage
if not "%~3"=="" goto usage

set "SRC=%~f1"
set "DST=%~f2"
if "%SRC:~-1%"=="\" set "SRC=%SRC:~0,-1%"
if "%DST:~-1%"=="\" set "DST=%DST:~0,-1%"

set "TOTAL=0"
set "SAME=0"
set "DIFF=0"
set "MISSING=0"
set "EXTRA=0"

if exist "%SRC%\" goto dirmode
if exist "%DST%\" goto mixed
goto filemode

rem ---- ファイル同士の比較 ----------------------------------
:filemode
echo ==========================================================
echo  A: %SRC%
echo  B: %DST%
echo ==========================================================
set "VERBOSE=1"
call :compare "%SRC%" "%DST%" "%~nx1"
goto result

rem ---- フォルダ同士の比較 ----------------------------------
:dirmode
if not exist "%DST%\" goto mixed
echo ==========================================================
echo  A: %SRC%\
echo  B: %DST%\
echo ==========================================================
set "VERBOSE="
for /r "%SRC%" %%F in (*) do (
    set "REL=%%F"
    set "REL=!REL:%SRC%\=!"
    call :compare "%%F" "%DST%\!REL!" "!REL!"
)
for /r "%DST%" %%F in (*) do (
    set "REL=%%F"
    set "REL=!REL:%DST%\=!"
    if not exist "%SRC%\!REL!" (
        set /a EXTRA+=1
        echo [EXTRA] !REL!  ^(A 側に存在しません^)
    )
)
goto result

rem ---- 1ファイル分の比較 -----------------------------------
:compare
set "F1=%~1"
set "F2=%~2"
set "LABEL=%~3"
set /a TOTAL+=1

if not exist "%F2%" (
    set /a MISSING+=1
    echo [NONE]  %LABEL%  ^(B 側に存在しません^)
    exit /b 0
)

set "S1=%~z1"
for %%I in ("%F2%") do set "S2=%%~zI"
if not "%S1%"=="!S2!" (
    set /a DIFF+=1
    echo [DIFF]  %LABEL%  ^(サイズ %S1% / !S2! バイト^)
    exit /b 0
)

fc /b "%F1%" "%F2%" >nul 2>&1
if errorlevel 1 (
    set /a DIFF+=1
    echo [DIFF]  %LABEL%  ^(内容が異なります^)
    if defined VERBOSE (
        set "FIRSTDIFF="
        for /f "usebackq delims=" %%L in (`fc /b "%F1%" "%F2%" ^| findstr /r /c:"^[0-9A-F][0-9A-F]*:"`) do (
            if not defined FIRSTDIFF set "FIRSTDIFF=%%L"
        )
        if defined FIRSTDIFF echo         最初の相違 ^(オフセット: A / B^): !FIRSTDIFF!
    )
    exit /b 0
)

set /a SAME+=1
echo [OK]    %LABEL%
exit /b 0

rem ---- 結果表示 --------------------------------------------
:result
echo.
echo ----------------------------------------------------------
echo  比較したファイル数 : %TOTAL%
echo  一致               : %SAME%
echo  不一致             : %DIFF%
echo  B 側に無い         : %MISSING%
if not "%EXTRA%"=="0" echo  A 側に無い         : %EXTRA%
echo ----------------------------------------------------------
set /a NG=DIFF+MISSING+EXTRA
if %TOTAL%==0 (
    echo 比較対象のファイルがありませんでした。
    set "RC=1"
) else if %NG%==0 (
    echo 結果: 完全に一致しました。
    set "RC=0"
) else (
    echo 結果: 一致しません。
    set "RC=1"
)
echo.
pause
exit /b %RC%

:mixed
echo ファイル同士、またはフォルダ同士でドロップしてください。
echo ^(ファイルとフォルダの組み合わせは比較できません^)
echo.
pause
exit /b 1

:usage
echo ==========================================================
echo  binary_compare.bat
echo ==========================================================
echo 比較したい 2 つのファイル、または 2 つのフォルダを
echo まとめてこのバッチファイルにドラッグ＆ドロップしてください。
echo.
echo コマンドラインからは次のように使えます:
echo   binary_compare.bat "A.mp4" "B.mp4"
echo   binary_compare.bat "A_dir" "B_dir"
echo.
pause
exit /b 1
