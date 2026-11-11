@echo off
chcp 65001 >nul
title Teyitli Firsat - site kurulumu
cd /d "%~dp0"
rem Program klasorunde "veri" klasoru varsa ayarlar ve veriler orada tutulur (tasinabilir kurulum)
if exist "%~dp0veri\" set "KASIM_FIRSAT_DIZIN=%~dp0veri"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python bulunamadi. Once Kasim-Firsatlari-Baslat.bat ile programi kurun.
  pause
  exit /b 1
)
%PY% kasim_firsat.py --yayin-kur
echo.
echo Kurulum bitti. Simdi Kasim-Firsatlari-Baslat.bat ile programi baslatin; site 10 dakikada bir guncellenir.
pause
