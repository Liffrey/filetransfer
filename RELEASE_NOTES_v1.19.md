# File Transfer Console v1.19

Tarih: 2026-10-14

## Yenilikler

- `robocopy` durdurma akışı Windows üzerinde daha güvenli hale getirildi.
- Alt süreç ağacı (`taskkill /T /F`) ile birlikte kapanır; ana transfer süreci kesildiğinde bırakılan robocopy süreçleri temiz şekilde sonlandırılır.
- Engine modülleri için import yolu sabitlenerek çalışma dizini bağımlılığı kaldırıldı.
- Çok sayıda dosya içeren transferlerde GUI güncellemeleri daha güvenli hale geldi.
- Progress/ETA ve dosya sayısı çıktısı daha okunabilir şekilde iyileştirildi.

## Düzeltmeler

- Kök dizin bağımlılığı nedeniyle oluşabilecek import hataları giderildi.
- `cancel_event` tetiklendiğinde transferin tam işlem ağacını öldürmesi garantilendi.
- Critical safety checks için regressiyon testleri eklendi.

## Doğrulama

- `pytest tests/test_progress.py` çalıştırıldı.
- Sonuç: 4 passed

## Yapı

- Tek dosya EXE üretimi için PyInstaller build süreci desteklenmektedir.
- Üretilen dosya: `dist/TransferConsole.exe`
