@echo off
rem Lance le brief quotidien (appelé chaque matin à 7h45 par le Planificateur de tâches Windows).
rem PYTHONPATH=C:\mi : contournement du paquet mistralai corrompu dans l'installation Python.
cd /d "%~dp0"
if not exist logs mkdir logs
set PYTHONPATH=C:\mi
set PYTHONUNBUFFERED=1
set PYTHONIOENCODING=utf-8
"C:\Users\melai\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.10_qbz5n2kfra8p0\python.exe" main.py --email --send >> logs\veille.log 2>&1
