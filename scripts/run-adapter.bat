@echo off
cd /d w:\projects\writing-assistant
.venv\Scripts\python.exe -u -m llm.run adapter > llm-output.log 2>&1
