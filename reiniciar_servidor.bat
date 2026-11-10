@echo off
chcp 65001 >nul
title SEGURANCA - reiniciar servidor
cd /d "%~dp0"
echo Encerrando o servidor atual...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'run.py' -and $_.CommandLine -notmatch 'powershell' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
timeout /t 3 /nobreak >nul
echo Iniciando o servidor...
set PYTHONIOENCODING=utf-8
start "SEGURANCA - servidor" /min py -3.13 run.py --sem-navegador
rem Analisador de marca/modelo dos carros: sobe se ainda nao estiver rodando.
powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'veiculos_modelo_rodar' })) { Start-Process -FilePath py -ArgumentList '-3.13','-m','scripts.veiculos_modelo_rodar' -WorkingDirectory '%~dp0' -WindowStyle Minimized }"
echo Aguardando o sistema subir...
powershell -NoProfile -Command "for($i=0;$i -lt 60;$i++){ Start-Sleep 3; try { Invoke-WebRequest http://127.0.0.1:8000/ -UseBasicParsing -TimeoutSec 3 | Out-Null; Start-Process 'http://localhost:8000/'; exit 0 } catch {} }; exit 1"
if errorlevel 1 (
  echo O servidor nao respondeu em 3 minutos. Veja a janela "SEGURANCA - servidor".
  pause
) else (
  echo Pronto: sistema no ar em http://localhost:8000
  timeout /t 5 >nul
)
