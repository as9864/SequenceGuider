@echo off
rem SequenceGuider web server behind Cloudflare Tunnel. Edit the four lines below, then
rem register it with Task Scheduler (docs/deploy-windows.md, step 3).
set REPO=C:\Users\YOU\projects\SequenceGuider
set HOME_DIR=D:\tango
set PUBLIC_URL=https://tango.example.com
set PORT=8765

cd /d "%REPO%"
rem 127.0.0.1: only cloudflared on this PC reaches the server; the internet comes in through the tunnel
uv run sequenceguider serve --home "%HOME_DIR%" --host 127.0.0.1 --port %PORT% ^
  --behind-proxy --public-url %PUBLIC_URL% --signup invite --max-jobs 1 --monthly-limit 4 --no-open ^
  >> "%HOME_DIR%\server.log" 2>&1
