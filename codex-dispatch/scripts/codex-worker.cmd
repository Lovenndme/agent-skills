@echo off
rem cmd / PowerShell entry for codex-worker; logic lives in codex-worker.py next to this file.
rem Keep this file ASCII-only: cmd parses .cmd files with the OEM code page (GBK here).
python -I -X utf8 "%~dp0codex-worker.py" %*
