@echo off
chcp 65001 >nul
title Kasim Firsatlari
cd /d "%~dp0"
rem Program klasorunde "veri" klasoru varsa ayarlar ve veriler orada tutulur (tasinabilir kurulum)
if exist "%~dp0veri\" set "KASIM_FIRSAT_DIZIN=%~dp0veri"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY goto python_yok

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
if errorlevel 1 goto python_yok

rem Eski surum hala aciksa (8765 portunu tutuyorsa) once onu kapat
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8765 .*LISTENING"') do (
  echo Acik kalan eski program kapatiliyor...
  taskkill /PID %%p /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

rem Istege bagli: magaza sayfalarini gercek Chrome gibi okuyan curl_cffi. Kurulamazsa program yine calisir.
%PY% -c "import curl_cffi" >nul 2>nul
if errorlevel 1 (
  echo Magaza okuma motoru kuruluyor, bir kereye mahsus...
  %PY% -m pip install --quiet --disable-pip-version-check curl_cffi >nul 2>&1
)

echo Kasim Firsatlari baslatiliyor...
%PY% "%~dp0kasim_firsat.py" --ag
echo.
echo Program kapandi. Pencereyi kapatmak icin bir tusa basin.
pause >nul
exit /b

:python_yok
echo.
echo Python bulunamadi (ya da surumu cok eski).
echo 1) Acilan sayfadan "Download Python" dugmesine basip Python'u indirin.
echo 2) Kurulumun ilk ekraninda "Add python.exe to PATH" kutusunu isaretleyin, "Install Now" deyin.
echo 3) Kurulum bitince bu dosyaya tekrar cift tiklayin.
start "" "https://www.python.org/downloads/windows/"
pause
