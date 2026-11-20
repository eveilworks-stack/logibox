"""PDFの取込と、保存済み添付ファイルの読出し。"""
import base64, hashlib, os, secrets

ALLOWED={'application/pdf':'.pdf','image/jpeg':'.jpg','image/png':'.png'}
MAX=8*1024*1024

def decode_pdf(value, mime):
    if mime != 'application/pdf':
        raise ValueError('新規添付・差し替えはPDFのみ対応しています')
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError('添付ファイルの形式が正しくありません') from exc
    if not raw or len(raw) > MAX:
        raise ValueError('添付ファイルは8MB以内にしてください')
    if not raw.startswith(b'%PDF-'):
        raise ValueError('ファイルの内容がPDF形式ではありません')
    return raw


def store(folder,raw,suffix):
    os.makedirs(folder,exist_ok=True)
    name=secrets.token_hex(18)+suffix
    path=os.path.join(folder,name)
    with open(path,'xb') as f:f.write(raw)
    return name

def safe_path(folder,name):
    if not name or '/' in name or '\\' in name or name in ('.','..'):raise ValueError('ファイルパスが不正です')
    return os.path.join(folder,name)


def load(folder, name):
    with open(safe_path(folder, name), 'rb') as file:
        return file.read()


def content_digest(raw):
    return hashlib.sha256(raw).digest()


def file_digest(folder, name):
    with open(safe_path(folder, name), 'rb') as file:
        digest = hashlib.sha256()
        for chunk in iter(lambda: file.read(128 * 1024), b''):
            digest.update(chunk)
        return digest.digest()


def render_page(page):
    from fitz import Matrix

    longest = max(page.rect.width, page.rect.height)
    if longest <= 0:
        raise ValueError('PDFのページ寸法が正しくありません')
    scale = min(3, 1600 / longest)
    return page.get_pixmap(matrix=Matrix(scale, scale), alpha=False, annots=True).tobytes('png')

