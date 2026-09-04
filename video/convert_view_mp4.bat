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
rem
rem  出力: <ファイル名>_h265.mp4
rem ==========================================================

rem ---- 設定 ------------------------------------------------
rem MODE=cq  : 画質基準。シーンによってサイズは変動する
rem MODE=vbr : BITRATE で狙ったファイルサイズにほぼ合わせる
set "MODE=vbr"

rem CQ: 小さいほど高画質・大サイズ。WQHD/30fps なら 22-26 が目安
set "CQ=24"

rem VBR 用。1時間の動画で 7M = 約3.1GB / 9M = 約4.0GB / 11M = 約4.9GB
set "BITRATE=9M"
set "MAXRATE=16M"
set "BUFSIZE=32M"

rem PRESET: p1(最速) - p7(最高画質)。最終出力なので画質優先で p7
set "PRESET=p7"
rem 10bit(Main 10)は 8bit よりバンディングが出にくく、同画質ならサイズも小さくなる
set "PROFILE=main10"
set "PIXFMT=p010le"

rem 画質補助オプション。b_ref_mode / temporal-aq は Turing 世代以降が必要
rem -g 30 は1秒ごとのキーフレーム。サイズは数%増えるがシークが速くなる
set "EXTRA=-tune hq -rc-lookahead 32 -bf 3 -b_ref_mode middle -spatial-aq 1 -aq-strength 8 -temporal-aq 1 -g 30"

set "AUDIO=-c:a aac -b:a 192k -ac 2"
rem ----------------------------------------------------------

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo ffmpeg が見つかりません。PATH を確認してください。
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

if /i "%MODE%"=="cq" (
    set "RATE=-rc vbr -cq %CQ% -b:v 0"
) else (
    rem multipass はエンコード自体は1回で、GPU 内部で2パス相当の解析を行う
    set "RATE=-rc vbr -multipass fullres -cq 0 -b:v %BITRATE% -maxrate %MAXRATE% -bufsize %BUFSIZE%"
)

set "FAILED="

:loop
if "%~1"=="" goto done

set "OUT=%~dp1%~n1_h265.mp4"

echo ==========================================================
echo  %~nx1  ^-^>  %~n1_h265.mp4   ^(NVENC / %MODE%^)
echo ==========================================================

if exist "%OUT%" (
    echo [SKIP] 出力先が既に存在します: %OUT%
    goto next
)

ffmpeg -hide_banner -loglevel warning -stats -i "%~f1" ^
    -map 0:v:0 -map 0:a? ^
    %VOPT% %RATE% ^
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
