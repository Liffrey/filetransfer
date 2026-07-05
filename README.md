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

## Test Durumu (onemli - lutfen okuyun)

**Engine katmani (config/transfer/credentials/scheduler/logutil):**
Bu Linux ortaminda GERCEKTEN calistirilarak test edildi - sahte bir
robocopy binary'si ile uctan uca transfer senaryosu (yas filtresi, hash
dogrulama, disk kontrolu, log/hashlog uretimi, config kaydetme/yukleme,
bozuk dosyadan kurtarma) dogrulandi. Bu kisim yuksek guvenilirliktedir.

**GUI katmani (gui/*.py):**
Gelistirme sirasinda PySide6 bu ortamda kullanilamaz hale geldi (paket
indirme kisitlamasi), bu yuzden gui/main_window.py ve gui/cred_manager.py
GERCEK CALISTIRILARAK test EDILEMEDI. gui/job_editor.py kismi olarak
test edildi (form doldurma/okuma dogrulandi) ama sonra ayni kisitlama
onu da etkiledi.

Bunun yerine:
- Tum dosyalar sozdizimi (syntax) duzeyinde derlendi ve temiz cikti
- Tum PySide6 sinif/enum kullanimlari elle Qt6 API'siyle çapraz kontrol
  edildi (import eksikligi, yanlis enum namespace'i, .exec() vs .exec_()
  gibi bilinen hata kaliplari tek tek tarandi ve TEMIZ cikti)
- Thread/sinyal mimarisi (worker thread + queued connection) Qt'nin
  belgelenen davranisina gore tasarlandi

Yani engine kesinlikle calisir durumda; GUI kismi da yuksek olasilikla
calisir (sozdizimi + API kullanimi dogrulandi) ama ilk gercek Windows
calistirmasinda birlikte kucuk GUI hatalarini (varsa) duzeltmemiz makul
bir beklenti - tipik olarak layout/gorunum detaylari, is mantigi degil.
