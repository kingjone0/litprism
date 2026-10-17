@echo off
REM Dev helper: let the shell launch the *source* sidecar instead of the packed exe.
REM Why: editing Python should not require a 2-minute PyInstaller rebuild, and we
REM      want the access log on disk as proof that the webview really called the API.
REM ASCII only -- cmd.exe reads .bat as GBK here, non-ASCII comments break it.
REM
REM Usage (bash):
REM   LITPRISM_SIDECAR=<repo>/packaging/_debug_sidecar.bat \
REM   LITPRISM_ACCESS_LOG=1 LITPRISM_LOG_LEVEL=info \
REM   electron desktop-electron/main.js
REM
REM NOTE: paths below are relative to this file. Do NOT hardcode an absolute repo
REM path -- a copied repo would then silently run the ORIGINAL source tree.

set "PYTHONPATH=%~dp0..\src"
set "PY=C:\Users\18434\.workbuddy\binaries\python\envs\default\Scripts\python.exe"

REM stdout must stay clean: the shell reads the handshake line from it.
REM Only stderr goes to the log file.
"%PY%" -m litprism.sidecar %* 2>>"%~dp0_sidecar_stderr.log"
