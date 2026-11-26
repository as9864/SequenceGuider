@echo off
rem Daily copy of accounts + practice records (records.db). Safe while the server runs.
rem BACKUP_DIR: preferably another disk or a synced folder (OneDrive, Google Drive).
set REPO=C:\Users\YOU\projects\SequenceGuider
set HOME_DIR=D:\tango
set BACKUP_DIR=%USERPROFILE%\OneDrive\sequenceguider-backups

cd /d "%REPO%"
uv run sequenceguider backup --home "%HOME_DIR%" --out "%BACKUP_DIR%" --keep 30 >> "%HOME_DIR%\backup.log" 2>&1
