# Veri Transfer Konsolu (Python + PySide6)

PowerShell/WinForms surumunun Python'a tam donusumu.

## Kurulum (gelistirme/calistirma)

```
pip install -r requirements.txt
python main.py
```

## Tek EXE'ye derleme

```
python build_exe.py
```

Cikti: `dist/TransferConsole.exe` — TEK dosya, baska hicbir dosyaya
(psm1, ayri ps1 script) ihtiyac duymaz.

## Gorev Zamanlayici (Scheduled Task) kullanimi

GUI icindeki "Zamanla" butonu otomatik olarak asagidaki komutu kaydeder:

```
TransferConsole.exe --run-job "JobAdi" --config "C:\ProgramData\DataTransferTool\jobs.json"
```

## Dosya Yapisi

```
main.py                  Giris noktasi (GUI + CLI mod)
engine/
  config.py               Job konfigurasyonu (jobs.json) - atomic yazma + yedekleme
  transfer.py             Robocopy + hash dogrulama + disk kontrolu ana motoru
  credentials.py          DPAPI (Windows) ile kimlik bilgisi saklama + SMB pre-auth
  scheduler.py            Windows Gorev Zamanlayici (schtasks) entegrasyonu
  logutil.py              Log yazimi, disk bilgisi, hash log uretimi
gui/
  main_window.py          Ana pencere (job listesi + canli log)
  job_editor.py           Yeni/Duzenle job dialog'u
  cred_manager.py         Kimlik bilgisi yoneticisi dialog'u
build_exe.py              PyInstaller derleme scripti
requirements.txt
```

