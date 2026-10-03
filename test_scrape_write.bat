@echo off
cd /d "%~dp0"
chcp 65001 >nul
title TPS 单页抓取与实时写库验证
echo ============================================================
echo   正在发起穿云单页采集并写入 MariaDB (people_search)...
echo ============================================================
.venv\Scripts\python.exe tools\test_live_scrape.py https://www.truepeoplesearch.com/find/person/px82l44nur68u2l8n60 --write
echo ============================================================
pause
