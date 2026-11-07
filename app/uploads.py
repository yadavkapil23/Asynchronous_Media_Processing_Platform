import hashlib
import io
import logging
from pathlib import Path

from PIL import Image
from sqlalchemy import update

from .config import settings
from .models import Upload

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 16_000_000
FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
log = logging.getLogger(__name__)


class UploadProblem(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def local_path(key):
    root = Path(settings.local_storage_path).resolve()
    relative = Path(key)
    if relative.is_absolute() or ".." in relative.parts or "\\" in key or ":" in key:
        raise UploadProblem(422, "Invalid storage key")
    candidate = root
    for part in relative.parts:
        candidate /= part
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            break
        if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise UploadProblem(422, "Storage links are unsupported")
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise UploadProblem(422, "Invalid storage key")
    return path


def check_size(length, expected=None):
    if length > MAX_BYTES:
        raise UploadProblem(413, "Maximum upload size is 20 MiB")
    if not length or (expected is not None and length != expected):
        raise UploadProblem(422, "File length is zero or differs from the declared size")


def validate_image(data, content_type):
    try:
        with Image.open(io.BytesIO(data), formats=list(FORMATS)) as image:
            if FORMATS[image.format] != content_type:
                raise UploadProblem(415, "Image format differs from the declared content type")
            if image.width * image.height > MAX_PIXELS:
                raise UploadProblem(413, "Image exceeds 16 million pixels")
            if getattr(image, "n_frames", 1) != 1:
                raise UploadProblem(415, "Animated images are unsupported")
            image.verify()
        with Image.open(io.BytesIO(data), formats=list(FORMATS)) as image:
            image.load()
    except UploadProblem:
        raise
    except Image.DecompressionBombError as exc:
        raise UploadProblem(413, "Image exceeds the pixel limit") from exc
    except (OSError, SyntaxError, ValueError, EOFError) as exc:
        raise UploadProblem(422, "Invalid or corrupt image") from exc


def read_bytes(upload):
    with local_path(upload.object_key).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    check_size(len(data), upload.size)
    return data


def remove_staging(key):
    local_path(key).unlink(missing_ok=True)


def discard_new_staging(upload_id, key):
    if key == f"staging/{upload_id}":
        try:
            remove_staging(key)
        except OSError:
            log.warning("Staging cleanup deferred for upload %s", upload_id)


def sealed(upload):
    return upload.status == "uploaded" and upload.object_key.startswith(f"sources/{upload.id}/")


def accept_upload(db, upload, data=None):
    local_put = data is not None
    if sealed(upload) and not local_put:
        return upload
    if upload.status != "pending" and (local_put or upload.status != "uploaded"):
        raise UploadProblem(409, "Upload cannot be finalized in its current state")
    upload_id, old_key = upload.id, upload.object_key
    claim = update(Upload).where(Upload.id == upload_id, Upload.object_key == old_key, Upload.status == upload.status)
    try:
        if data is None:
            data = read_bytes(upload)
        check_size(len(data), upload.size)
        validate_image(data, upload.content_type)
    except UploadProblem:
        if not local_put and old_key == f"staging/{upload_id}":
            rejected = db.execute(claim.values(status="rejected"))
            db.commit()
            if rejected.rowcount == 1:
                discard_new_staging(upload_id, old_key)
        raise
    digest = hashlib.sha256(data).hexdigest()
    key = f"sources/{upload_id}/{digest}"
    changed = db.execute(claim.values(object_key=key, size=len(data), status="uploaded"))
    if changed.rowcount != 1:
        db.rollback()
        winner = db.get(Upload, upload_id)
        if not local_put and winner is not None and sealed(winner):
            return winner
        raise UploadProblem(409, "Upload already finalized")
    try:
        path = local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        db.commit()
    except Exception:
        db.rollback()
        raise
    discard_new_staging(upload_id, old_key)
    return db.get(Upload, upload_id)


def worker_source(db, upload):
    if upload is None or upload.status != "uploaded":
        raise UploadProblem(409, "Upload is unavailable")
    if not sealed(upload):
        upload = accept_upload(db, upload)
    data = read_bytes(upload)
    if upload.object_key != f"sources/{upload.id}/{hashlib.sha256(data).hexdigest()}":
        raise UploadProblem(422, "Source checksum differs from the verified image")
    return io.BytesIO(data)
