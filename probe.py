"""Измерение громкости mp3: средний RMS и пик в dBFS.

Два источника данных:
  * на телефоне — системный декодер Android (MediaCodec) через pyjnius,
    ничего доустанавливать не нужно, декодирование аппаратное;
  * на компьютере — ffmpeg, чтобы ту же самую логику можно было проверять
    и отлаживать без телефона.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import numpy as _np
except ImportError:  # numpy необязателен, ниже есть запасной путь
    _np = None


@dataclass
class Measurement:
    """Уровень трека в цифровой шкале."""

    rms_dbfs: float
    peak_dbfs: float


class ProbeError(RuntimeError):
    """Не удалось измерить громкость файла."""


_SILENCE = Measurement(rms_dbfs=-120.0, peak_dbfs=-120.0)


def _levels_from_pcm16(total_squares: float, samples: int, peak: int) -> Measurement:
    """Переводит накопленные суммы 16-битных отсчётов в dBFS."""
    if samples == 0 or peak == 0:
        return _SILENCE
    full_scale = 32768.0
    rms = math.sqrt(total_squares / samples) / full_scale
    return Measurement(
        rms_dbfs=20 * math.log10(max(rms, 1e-9)),
        peak_dbfs=20 * math.log10(min(peak, 32768) / full_scale),
    )


# ---------------------------------------------------------------------------
# Android: MediaExtractor + MediaCodec
# ---------------------------------------------------------------------------

class AndroidProbe:
    """Декодирует mp3 системным кодеком Android и считает уровень."""

    name = "MediaCodec"

    def __init__(self) -> None:
        from jnius import autoclass  # импорт только на телефоне

        self._MediaExtractor = autoclass("android.media.MediaExtractor")
        self._MediaCodec = autoclass("android.media.MediaCodec")
        self._BufferInfo = autoclass("android.media.MediaCodec$BufferInfo")
        self._MediaFormat = autoclass("android.media.MediaFormat")

    def measure(self, path: Path) -> Measurement:
        extractor = self._MediaExtractor()
        codec = None
        try:
            extractor.setDataSource(str(path))
            track = self._select_audio_track(extractor)
            fmt = extractor.getTrackFormat(track)
            extractor.selectTrack(track)

            codec = self._MediaCodec.createDecoderByType(
                fmt.getString(self._MediaFormat.KEY_MIME))
            codec.configure(fmt, None, None, 0)
            codec.start()
            return self._decode_loop(extractor, codec)
        except Exception as exc:  # noqa: BLE001 — наружу отдаём одну понятную ошибку
            raise ProbeError(f"{type(exc).__name__}: {exc}") from exc
        finally:
            if codec is not None:
                try:
                    codec.stop()
                    codec.release()
                except Exception:
                    pass
            try:
                extractor.release()
            except Exception:
                pass

    def _select_audio_track(self, extractor) -> int:
        for i in range(extractor.getTrackCount()):
            mime = extractor.getTrackFormat(i).getString(self._MediaFormat.KEY_MIME)
            if mime and mime.startswith("audio/"):
                return i
        raise ProbeError("в файле нет звуковой дорожки")

    def _decode_loop(self, extractor, codec) -> Measurement:
        info = self._BufferInfo()
        end_of_stream = self._MediaCodec.BUFFER_FLAG_END_OF_STREAM
        squares = 0.0
        samples = 0
        peak = 0
        fed_all = False
        drained_all = False

        while not drained_all:
            if not fed_all:
                in_index = codec.dequeueInputBuffer(10000)
                if in_index >= 0:
                    buf = codec.getInputBuffer(in_index)
                    size = extractor.readSampleData(buf, 0)
                    if size < 0:
                        codec.queueInputBuffer(in_index, 0, 0, 0, end_of_stream)
                        fed_all = True
                    else:
                        codec.queueInputBuffer(in_index, 0, size,
                                               extractor.getSampleTime(), 0)
                        extractor.advance()

            out_index = codec.dequeueOutputBuffer(info, 10000)
            if out_index >= 0:
                if info.size > 0:
                    pcm = _bytebuffer_to_bytes(codec.getOutputBuffer(out_index),
                                               info.offset, info.size)
                    chunk_sq, chunk_n, chunk_peak = _accumulate_pcm16(pcm)
                    squares += chunk_sq
                    samples += chunk_n
                    peak = max(peak, chunk_peak)
                codec.releaseOutputBuffer(out_index, False)
                if info.flags & end_of_stream:
                    drained_all = True
            elif out_index == -1 and fed_all:
                # таймаут после того, как весь вход скормлен: кодек молчит
                continue

        return _levels_from_pcm16(squares, samples, peak)


def _bytebuffer_to_bytes(buf, offset: int, size: int) -> bytes:
    """Копирует кусок java.nio.ByteBuffer в питоновские байты."""
    buf.position(offset)
    chunk = bytearray(size)
    try:
        buf.get(chunk)          # pyjnius переносит данные обратно в bytearray
        return bytes(chunk)
    except Exception:
        buf.position(offset)    # запасной, медленный путь
        return bytes(buf.get() & 0xFF for _ in range(size))


def _accumulate_pcm16(pcm: bytes) -> tuple[float, int, int]:
    """Сумма квадратов, число отсчётов и пик для блока 16-битного PCM."""
    usable = len(pcm) - (len(pcm) % 2)
    if usable == 0:
        return 0.0, 0, 0
    if _np is not None:
        arr = _np.frombuffer(pcm[:usable], dtype="<i2").astype(_np.float64)
        return float((arr * arr).sum()), arr.size, int(_np.abs(arr).max())
    import array
    arr = array.array("h")
    arr.frombytes(pcm[:usable])
    total = 0.0
    peak = 0
    for value in arr:
        total += float(value) * value
        if abs(value) > peak:
            peak = abs(value)
    return total, len(arr), peak


# ---------------------------------------------------------------------------
# Компьютер: ffmpeg
# ---------------------------------------------------------------------------

_RE_MEAN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")
_RE_MAX = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB")
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class FfmpegProbe:
    """Измерение через ffmpeg — используется при отладке на компьютере."""

    name = "ffmpeg"

    def __init__(self, ffmpeg: str | None = None) -> None:
        self.ffmpeg = ffmpeg or os.environ.get("FFMPEG_BINARY") or shutil.which("ffmpeg")
        if not self.ffmpeg:
            raise ProbeError("ffmpeg не найден (нужен только для запуска на ПК)")

    def measure(self, path: Path) -> Measurement:
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-i", str(path),
             "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", creationflags=_CREATE_NO_WINDOW,
        )
        mean = _RE_MEAN.search(result.stderr or "")
        if not mean:
            raise ProbeError("ffmpeg не смог измерить файл")
        peak = _RE_MAX.search(result.stderr or "")
        return Measurement(float(mean.group(1)),
                           float(peak.group(1)) if peak else 0.0)


def make_probe(ffmpeg: str | None = None):
    """Возвращает измеритель, подходящий для текущей системы."""
    if os.environ.get("ANDROID_ARGUMENT") or os.environ.get("ANDROID_ROOT"):
        return AndroidProbe()
    return FfmpegProbe(ffmpeg)
