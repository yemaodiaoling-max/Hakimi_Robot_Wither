"""Small encrypted vault for the Linux host; Windows keeps its existing DPAPI."""
import os
from pathlib import Path


def crypt(data,protect):
    from cryptography.fernet import Fernet
    path=Path(os.environ.get('HAKIMI_VAULT_KEY_FILE',str(Path.home()/'.config/hakimi/vault.key')))
    if protect and not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        try:
            with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as stream:
                stream.write(Fernet.generate_key())
        except FileExistsError:pass
    vault=Fernet(path.read_bytes())
    return vault.encrypt(data) if protect else vault.decrypt(data)
