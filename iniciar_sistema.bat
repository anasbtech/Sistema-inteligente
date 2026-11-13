@echo off
chcp 65001 >nul
title AizonVMS - servidor
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
rem Analisador de marca/modelo dos carros: sobe se ainda nao estiver rodando.
powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'veiculos_modelo_rodar' })) { Start-Process -FilePath py -ArgumentList '-3.13','-m','scripts.veiculos_modelo_rodar' -WorkingDirectory '%~dp0' -WindowStyle Hidden }"
rem Servidor (fica nesta janela; fechar a janela desliga o sistema).
py -3.13 run.py --sem-navegador > logs\servidor_stdout.log 2> logs\servidor_stderr.log
