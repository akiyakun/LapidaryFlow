@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul

rem ==========================================================
rem  convert_view_mp4.bat
rem  結合済みの ProRes / FFV1 などのマスターを
rem  視聴用の H.265 (HEVC / Main 10) mp4 に変換します。
rem  エンコードは NVIDIA NVENC (hevc_nvenc) を使用します。
rem  動画ファイルをこのバッチにドラッグ＆ドロップしてください。
rem  複数ファイルの同時ドロップにも対応しています。
rem  入力の fps を判定して、ビットレートとキーフレーム間隔を自動調整します。
rem
rem  出力: <ファイル名>_h265.mp4
rem ==========================================================

rem ---- 設定 ------------------------------------------------
rem MODE=cq  : 画質基準。シーンによってサイズは変動する
rem MODE=vbr : BITRATE で狙ったファイルサイズにほぼ合わせる
set "MODE=vbr"

rem CQ: 小さいほど高画質・大サイズ。WQHD なら 22-26 が目安
rem cq モードは fps が上がるとサイズが増える代わりに画質が落ちない
set "CQ=24"

rem VBR 用。30fps 基準のビットレート(kbps)
rem 1時間の動画で 7000 = 約3.1GB / 9000 = 約4.0GB / 11000 = 約4.9GB
set "BITRATE_30=9000"

rem 高フレームレート素材の倍率(%)。fps は入力から自動判定する
rem 60fps は 30fps と同画質にするのに約1.4倍必要 (フレーム間が似ているので2倍は不要)
set "SCALE_50=125"
set "SCALE_60=140"
set "SCALE_120=180"

rem MAXRATE / BUFSIZE はビットレートからの倍率(%)で決める
set "MAXRATE_PCT=175"
set "BUFSIZE_PCT=350"

rem PRESET: p1(最速) - p7(最高画質)。最終出力なので画質優先で p7
set "PRESET=p7"
rem 10bit(Main 10)は 8bit よりバンディングが出にくく、同画質ならサイズも小さくなる
set "PROFILE=main10"
set "PIXFMT=p010le"

rem 画質補助オプション。b_ref_mode / temporal-aq は Turing 世代以降が必要
rem キーフレーム間隔 -g は fps に合わせて1秒ごとに自動設定する
set "EXTRA=-tune hq -rc-lookahead 32 -bf 3 -b_ref_mode middle -spatial-aq 1 -aq-strength 8 -temporal-aq 1"

set "AUDIO=-c:a aac -b:a 192k -ac 2"
rem ----------------------------------------------------------

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg が見つかりません。PATH を確認してください。
    echo.
    pause
    exit /b 1
)

where ffprobe >nul 2>&1
if errorlevel 1 (
    echo ffprobe が見つかりません。PATH を確認してください。
    echo.
    pause
    exit /b 1
)

ffmpeg -hide_banner -encoders 2>nul | findstr /c:"hevc_nvenc" >nul
if errorlevel 1 (
    echo この ffmpeg は hevc_nvenc に対応していません。
    echo NVENC 対応ビルドの ffmpeg を用意してください。
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

set "VOPT=-c:v hevc_nvenc -preset %PRESET% -profile:v %PROFILE% -pix_fmt %PIXFMT% -tag:v hvc1 %EXTRA%"
set "MUX=-movflags +faststart"

set "FAILED="

:loop
if "%~1"=="" goto done

set "OUT=%~dp1%~n1_h265.mp4"

call :getfps "%~f1"
call :setrate

echo ==========================================================
echo  %~nx1  ^-^>  %~n1_h265.mp4   ^(NVENC / %MODE% / %FPS%fps^)
echo ==========================================================

if exist "%OUT%" (
    echo [SKIP] 出力先が既に存在します: %OUT%
    goto next
)

ffmpeg -hide_banner -loglevel warning -stats -i "%~f1" ^
    -map 0:v:0 -map 0:a? ^
    %VOPT% -g %GOP% %RATE% ^
    %AUDIO% %MUX% "%OUT%"

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
exit /b 0

rem ---- サブルーチン ----------------------------------------

rem %1 の映像ストリームから fps を求めて FPS にセットする
:getfps
set "FPS=30"
set "RFR="
for /f "usebackq delims=" %%A in (`ffprobe -v error -select_streams v:0 -show_entries stream^=r_frame_rate -of csv^=p^=0 %1`) do set "RFR=%%A"
if not defined RFR goto :eof
set "NUM="
set "DEN="
for /f "tokens=1,2 delims=/" %%A in ("%RFR%") do (
    set "NUM=%%A"
    set "DEN=%%B"
)
if not defined DEN set "DEN=1"
rem N/A などの非数値が来ると set /a が 0 除算で落ちるので先に弾く
echo %NUM%| findstr /r "^[0-9][0-9]*$" >nul || goto :eof
echo %DEN%| findstr /r "^[1-9][0-9]*$" >nul || goto :eof
rem 60000/1001 のような分数を四捨五入して整数 fps にする
set /a FPS=(%NUM% + %DEN% / 2) / %DEN%
if %FPS% lss 1 set "FPS=30"
if %FPS% gtr 480 set "FPS=30"
goto :eof

rem FPS からキーフレーム間隔とレート制御オプションを組み立てる
:setrate
set "GOP=%FPS%"
set "SCALE=100"
if %FPS% geq 45 set "SCALE=%SCALE_50%"
if %FPS% geq 55 set "SCALE=%SCALE_60%"
if %FPS% geq 100 set "SCALE=%SCALE_120%"
set /a VBITRATE=%BITRATE_30% * %SCALE% / 100
set /a VMAXRATE=%VBITRATE% * %MAXRATE_PCT% / 100
set /a VBUFSIZE=%VBITRATE% * %BUFSIZE_PCT% / 100
if /i "%MODE%"=="cq" (
    set "RATE=-rc vbr -cq %CQ% -b:v 0"
) else (
    rem multipass はエンコード自体は1回で、GPU 内部で2パス相当の解析を行う
    set "RATE=-rc vbr -multipass fullres -cq 0 -b:v %VBITRATE%k -maxrate %VMAXRATE%k -bufsize %VBUFSIZE%k"
)
goto :eof
