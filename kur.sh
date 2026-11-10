#!/data/data/com.termux/files/usr/bin/bash
# Kasım Fırsatları — Termux için tek komutla kurulum
set -e
echo "== Kasım Fırsatları kuruluyor =="
DIZIN="$(cd "$(dirname "$0")" && pwd)"

if ! command -v python >/dev/null 2>&1; then
  echo "-> Python kuruluyor (birkaç dakika sürebilir)..."
  yes | pkg install python
fi

cp "$DIZIN/kasim_firsat.py" "$HOME/kasim_firsat.py"
python "$HOME/kasim_firsat.py" --kisayol >/dev/null 2>&1 || true

if ! grep -q "alias firsat=" "$HOME/.bashrc" 2>/dev/null; then
  echo 'alias firsat="python ~/kasim_firsat.py"' >> "$HOME/.bashrc"
fi

echo ""
echo "Kurulum tamam. Bundan sonra Termux'u açıp sadece:  firsat  yazman yeterli."
echo "Program şimdi başlıyor..."
echo ""
exec python "$HOME/kasim_firsat.py"
