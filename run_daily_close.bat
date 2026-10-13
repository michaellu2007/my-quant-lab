@echo off
rem 收盘后跑持仓决策清单（无窗口）。挂 Windows 任务计划：
rem   schtasks /create /tn "Qbot收盘清单" /tr "E:\QUANT\run_daily_close.bat" /sc weekly /d MON,TUE,WED,THU,FRI /st 16:30
rem 手动跑：直接双击本文件，或命令行运行后看 packs\Qbot\qbot\gui\bkt_result\signal_report.txt
cd /d E:\QUANT\packs\Qbot
venv39\Scripts\python.exe E:\QUANT\scripts\daily_close.py
