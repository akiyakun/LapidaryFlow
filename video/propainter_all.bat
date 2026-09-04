@rem うえからした (sjis認識させる呪文)
@echo off
chcp 932 >nul
pushd "%~dp0"

rem ==========================================================
rem  propainter_all.bat
rem  切り出し → ProPainter → 合成 を続けて実行します。
rem  mask_plan.json をこのバッチにドラッグ＆ドロップしてください。
rem  元動画が JSON に書いた場所に無いときは、
rem  JSON と元動画を一緒にドロップしてください。
rem
rem  途中で失敗したらそこで止まります。設定は各バッチ側で変えてください。
rem  何時間もかかる処理なので、先に propainter_compose.bat の
rem  PREFLIGHT=1 で境界を確認しておくと安全です。
rem ==========================================================

if "%~1"=="" (
    echo mask_plan.json をこのバッチファイルにドラッグ＆ドロップしてください。
    goto fail
)

echo ==========================================================
echo  1/3 切り出し
echo ==========================================================
call "%~dp0propainter_extract.bat" %*
if errorlevel 1 goto fail

echo ==========================================================
echo  2/3 ProPainter
echo ==========================================================
call "%~dp0propainter_run.bat" %*
if errorlevel 1 goto fail

echo ==========================================================
echo  3/3 合成
echo ==========================================================
call "%~dp0propainter_compose.bat" %*
if errorlevel 1 goto fail

echo.
echo すべて完了しました。
echo 字幕やチャプターを戻すときは merge_tracks.bat を使ってください。
echo.
pause
popd
exit /b 0

:fail
echo.
echo 処理を中断しました。
echo.
pause
popd
exit /b 1
