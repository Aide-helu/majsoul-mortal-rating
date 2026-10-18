@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 雀魂对手 rating 检讨
setlocal

rem 找到可用的 python (没装 playwright 的话会在 .venv 里自动装, 只装一次)
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"`) do set "PY=%%P"
if not defined PY (
  echo.
  echo  环境准备失败, 按上面的提示处理后再双击一次.
  pause
  exit /b 1
)

echo 统计自己的最近对局中，上家、对家、下家的 Mortal 评分。
echo.
set /p "WHO=请输入自己的雀魂 UID 或昵称: "
if not defined WHO goto :noinput
set /p "GAMES=最近多少局 (直接回车 = 20): "
if not defined GAMES set "GAMES=20"
set /p "MODE=四人场次 王座/玉/金/王东/玉东/金东 (直接回车 = 玉): "
if not defined MODE set "MODE=玉"
echo.
"%PY%" mahjong_rating_rival.py --id "%WHO%" --games %GAMES% --mode "%MODE%"
echo.
echo 结果位于 rating_out 下对应昵称的 _rival 文件夹中。
pause
exit /b

:noinput
echo 没有输入，已退出。
pause
exit /b 1
