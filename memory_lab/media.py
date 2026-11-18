from __future__ import annotations

from io import BytesIO
from pathlib import Path
import hashlib
import wave

from PIL import Image, UnidentifiedImageError
import mutagen

from memory_core.paths import guarded
from memory_core.types import MemoryFault, uid

IMAGE_TYPES = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png"), "WEBP": ("image/webp", ".webp")}


def inspect_media(content: bytes, name: str):
    suffix = Path(name).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".webp"):
        if len(content) > 20*1024*1024:
            raise MemoryFault("图片不能超过 20 MiB", 413)
        try:
            Image.MAX_IMAGE_PIXELS = 40_000_000
            with Image.open(BytesIO(content)) as image:
                fmt = image.format
                if fmt not in IMAGE_TYPES or image.width*image.height > Image.MAX_IMAGE_PIXELS:
                    raise MemoryFault("图片格式或像素数不支持")
                image.verify()
            mime, suffix = IMAGE_TYPES[fmt]
            return mime, suffix, None
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise MemoryFault("图片格式或文件内容无效") from error
    if suffix not in (".wav", ".mp3", ".m4a", ".ogg"):
        raise MemoryFault("仅支持 JPEG/PNG/WebP 图片和 WAV/MP3/M4A/OGG 语音")
    if len(content) > 25*1024*1024:
        raise MemoryFault("语音不能超过 25 MiB", 413)
    try:
        if suffix == ".wav":
            with wave.open(BytesIO(content), "rb") as audio:
                duration = audio.getnframes()/audio.getframerate()
            mime = "audio/wav"
        else:
            audio = mutagen.File(BytesIO(content))
            if not audio or not audio.info:
                raise MemoryFault("无法解析语音格式")
            duration = audio.info.length
            mime = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}[suffix]
            expected = {".mp3": "MP3", ".m4a": "MP4", ".ogg": "Ogg"}[suffix]
            if expected not in type(audio).__name__:
                raise MemoryFault("语音后缀与实际格式不符")
        if not 0 < duration <= 600:
            raise MemoryFault("语音时长须在 0 到 10 分钟之间")
        return mime, suffix, duration
    except (wave.Error, EOFError, OSError, mutagen.MutagenError, ZeroDivisionError) as error:
        raise MemoryFault("语音内容无效") from error


class Uploads:
    def __init__(self, project: Path, run_id: str):
        self.project = project
        self.root = guarded(project, ".local", "tmp", run_id, "uploads")
        self.root.mkdir(parents=True, exist_ok=True)
        self.items = {}

    def put(self, scope, content: bytes, name: str):
        mime, suffix, duration = inspect_media(content, name)
        attachment_id = uid("att")
        path = guarded(self.root, attachment_id+suffix)
        path.write_bytes(content)
        item = {"id": attachment_id, "path": str(path), "user_id": scope.user_id, "session_id": scope.session_id,
                "mime": mime, "suffix": suffix, "size": len(content), "duration": duration, "sha256": hashlib.sha256(content).hexdigest()}
        self.items[attachment_id] = item
        return {key: item[key] for key in ("id", "mime", "size", "duration")}

    def take(self, scope, ids):
        if len(ids) > 6 or len(set(ids)) != len(ids):
            raise MemoryFault("每条消息最多 6 个不重复附件")
        result = []
        for aid in ids:
            item = self.items.get(aid)
            if not item or item["user_id"] != scope.user_id or item["session_id"] != scope.session_id:
                raise MemoryFault("临时附件不存在或不属于当前会话", 404)
            result.append(item)
        return result

    def consume(self, ids):
        for aid in ids:
            self.items.pop(aid, None)

    def clear(self):
        for item in list(self.items.values()):
            guarded(self.root, Path(item["path"]).name).unlink(missing_ok=True)
        self.items.clear()
