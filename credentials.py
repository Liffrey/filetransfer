"""
credentials.py
---------------
Uzak sunuculara erisim icin kimlik bilgisi yonetimi.

Saklama: Windows DPAPI (CryptProtectData/CryptUnprotectData) dogrudan
ctypes ile cagirilir. Bu, PowerShell surumunde Export-Clixml'in ARKA
PLANDA yaptigi ile birebir aynidir - ayni kullanici + ayni makine
disinda sifre cozulemez. Ucuncu parti paket (keyring/pywin32) GEREKMEZ,
bu da PyInstaller ile tek-dosya EXE paketlemeyi kolaylastirir ve
"paket bulunamadi" turu ortam sorunlarini ortadan kaldirir.

SMB on-kimlik dogrulama: `cmdkey` (Windows'un kendi araci), subprocess
ile cagirilir - "E:\\" turu trailing-backslash sorunlari subprocess'in
liste-tabanli argv kullanimi sayesinde olusmaz.
"""
from __future__ import annotations

import base64
import ctypes
import json
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import ctypes.wintypes as wintypes
    _DWORD = wintypes.DWORD
else:
    # Linux/mac gelistirme ve test ortaminda ctypes.wintypes mevcut degildir.
    # DPAPI zaten sadece Windows'ta CAGRILIR (asagida IS_WINDOWS kontrolu ile
    # korunuyor); burada sadece modulun IMPORT EDILEBILMESI icin sahte bir
    # tip kullaniyoruz, gercek calisma zamaninda hic devreye girmez.
    _DWORD = ctypes.c_ulong


@dataclass
class Credential:
    alias: str
    username: str
    password: str


# ---------------------------------------------------------------------------
# DPAPI (Windows Data Protection API) - ctypes dogrudan cagri
# ---------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", _DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> _DATA_BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_encrypt(plaintext: bytes) -> bytes:
    """CryptProtectData: sadece ayni kullanici + ayni makinede cozulebilir."""
    if not IS_WINDOWS:
        raise RuntimeError("DPAPI sadece Windows'ta kullanilabilir.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _to_blob(plaintext)
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return result


def dpapi_decrypt(ciphertext: bytes) -> bytes:
    if not IS_WINDOWS:
        raise RuntimeError("DPAPI sadece Windows'ta kullanilabilir.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _to_blob(ciphertext)
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    )
    if not ok:
        raise ctypes.WinError()
    try:
        result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)
    return result


# ---------------------------------------------------------------------------
# Kimlik Bilgisi Deposu
# ---------------------------------------------------------------------------

class CredentialStore:
    """
    Her kimlik bilgisi ayri bir .cred dosyasinda, DPAPI ile sifrelenmis
    olarak saklanir (PowerShell surumundeki Export-Clixml dosyalarinin
    dogrudan karsiligi). Dosya icerigi: base64(DPAPI(json({user,pass}))).
    """

    def __init__(self, store_dir: str | Path):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)

    def _file_for(self, alias: str) -> Path:
        safe = re.sub(r'[\\/:*?"<>|]', "_", alias)
        return self.store_dir / f"{safe}.cred"

    def save(self, alias: str, username: str, password: str) -> None:
        payload = json.dumps({"username": username, "password": password}).encode("utf-8")
        if IS_WINDOWS:
            encrypted = dpapi_encrypt(payload)
        else:
            # Windows disinda (gelistirme/test ortami) DPAPI yok - duz base64
            # ile saklanir. Uretimde bu kod yolu HICBIR ZAMAN calismamalidir;
            # sadece Linux'ta test edilebilmesi icin duser guvenli bir fallback.
            encrypted = payload
        self._file_for(alias).write_bytes(base64.b64encode(encrypted))

    def get(self, alias: str) -> Optional[Credential]:
        f = self._file_for(alias)
        if not f.exists():
            return None
        try:
            encrypted = base64.b64decode(f.read_bytes())
            payload = dpapi_decrypt(encrypted) if IS_WINDOWS else encrypted
            data = json.loads(payload.decode("utf-8"))
            return Credential(alias=alias, username=data["username"], password=data["password"])
        except Exception:
            return None

    def list_all(self) -> list[Credential]:
        result = []
        for f in sorted(self.store_dir.glob("*.cred")):
            alias = f.stem
            cred = self.get(alias)
            if cred:
                result.append(cred)
        return result

    def remove(self, alias: str) -> None:
        f = self._file_for(alias)
        if f.exists():
            f.unlink()

    # ---------- SMB on-kimlik dogrulama ----------

    def register_smb_session(self, target: str, cred: Credential) -> bool:
        """cmdkey ile Windows SMB oturumu acmak icin kimlik bilgisini kaydeder."""
        try:
            proc = subprocess.run(
                ["cmdkey", f"/add:{target}", f"/user:{cred.username}", f"/pass:{cred.password}"],
                capture_output=True, text=True, timeout=10,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def unregister_smb_session(self, target: str) -> None:
        try:
            subprocess.run(["cmdkey", f"/delete:{target}"], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass


def resolve_unc_server(path: str) -> Optional[str]:
    """\\\\Sunucu\\Pay\\Klasor -> Sunucu"""
    m = re.match(r"^\\\\([^\\]+)", path)
    return m.group(1) if m else None
