"""Изменение громкости mp3 без перекодирования — метод утилиты mp3gain.

В каждом кадре MPEG Layer III есть поле global_gain: показатель степени,
с которым декодер восстанавливает спектр. Шаг поля равен 1.5 дБ, поэтому
громкость всего файла меняется простым прибавлением одного и того же числа
ко всем global_gain. Аудиоданные не пересжимаются — качество не теряется,
теги ID3 и обложка остаются нетронутыми (правятся только байты кадров).

На Android это единственный разумный путь: MP3-энкодера в системе нет,
а тащить в APK ffmpeg — это лишние 30 МБ и запуск чужого бинарника.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Шаг поля global_gain в децибелах (задан стандартом ISO 11172-3).
STEP_DB = 1.5

#: Битрейты Layer III, кбит/с: для MPEG1 и для MPEG2/2.5.
_BITRATES_V1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_BITRATES_V2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)

#: Частоты дискретизации по номеру версии MPEG (3=MPEG1, 2=MPEG2, 0=MPEG2.5).
_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}

_LAYER_III = 1  # значение поля layer для Layer III


class Mp3Error(ValueError):
    """Файл не похож на mp3 или повреждён."""


@dataclass
class Frame:
    """Кадр MPEG Layer III."""

    offset: int          # смещение заголовка в файле
    length: int          # полная длина кадра в байтах
    version: int         # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    channels: int        # 1 или 2
    protected: bool      # есть ли 16-битная CRC после заголовка
    sample_rate: int
    bitrate_kbps: int

    @property
    def side_info_offset(self) -> int:
        """Смещение служебной части кадра, в которой лежит global_gain."""
        return self.offset + 4 + (2 if self.protected else 0)


@dataclass
class GainReport:
    """Что получилось при изменении громкости."""

    frames: int = 0        # разобрано кадров
    changed: int = 0       # изменено полей global_gain
    clamped: int = 0       # полей, упёршихся в границу 0..255
    steps: int = 0         # применённый сдвиг в шагах
    sample_rate: int | None = None
    bitrate_kbps: int | None = None
    channels: int | None = None

    @property
    def applied_db(self) -> float:
        return self.steps * STEP_DB


# ---------------------------------------------------------------------------
# Работа с битовыми полями
# ---------------------------------------------------------------------------

def _get_bits(data: bytes, bit_pos: int, nbits: int) -> int:
    byte_i, off = divmod(bit_pos, 8)
    nbytes = (off + nbits + 7) // 8
    chunk = int.from_bytes(data[byte_i:byte_i + nbytes], "big")
    shift = nbytes * 8 - off - nbits
    return (chunk >> shift) & ((1 << nbits) - 1)


def _set_bits(data: bytearray, bit_pos: int, nbits: int, value: int) -> None:
    byte_i, off = divmod(bit_pos, 8)
    nbytes = (off + nbits + 7) // 8
    chunk = int.from_bytes(data[byte_i:byte_i + nbytes], "big")
    shift = nbytes * 8 - off - nbits
    mask = ((1 << nbits) - 1) << shift
    chunk = (chunk & ~mask) | ((value & ((1 << nbits) - 1)) << shift)
    data[byte_i:byte_i + nbytes] = chunk.to_bytes(nbytes, "big")


# ---------------------------------------------------------------------------
# Разбор кадров
# ---------------------------------------------------------------------------

def skip_id3v2(data: bytes) -> int:
    """Возвращает смещение первого аудиокадра, пропуская тег ID3v2."""
    if len(data) >= 10 and data[:3] == b"ID3":
        size = 0
        for byte in data[6:10]:
            size = (size << 7) | (byte & 0x7F)  # синхробезопасное число
        offset = 10 + size
        if data[5] & 0x10:  # присутствует футер
            offset += 10
        return min(offset, len(data))
    return 0


def parse_header(data: bytes, pos: int) -> Frame | None:
    """Разбирает заголовок кадра по смещению pos, если он там действительно есть."""
    if pos + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[pos:pos + 4]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None

    version = (b1 >> 3) & 0x03      # 1 — зарезервировано
    layer = (b1 >> 1) & 0x03
    if version == 1 or layer != _LAYER_III:
        return None

    protected = not (b1 & 0x01)
    bitrate_index = b2 >> 4
    sr_index = (b2 >> 2) & 0x03
    padding = (b2 >> 1) & 0x01
    if bitrate_index in (0, 15) or sr_index == 3:
        return None

    bitrate = (_BITRATES_V1 if version == 3 else _BITRATES_V2)[bitrate_index]
    sample_rate = _SAMPLE_RATES[version][sr_index]
    channels = 1 if (b3 >> 6) == 3 else 2

    samples = 1152 if version == 3 else 576
    length = (samples // 8) * bitrate * 1000 // sample_rate + padding
    if length < 24 or pos + length > len(data):
        return None

    return Frame(pos, length, version, channels, protected, sample_rate, bitrate)


def iter_frames(data: bytes, start: int | None = None):
    """Идёт по кадрам файла; при мусоре в потоке ищет следующую синхрометку."""
    pos = skip_id3v2(data) if start is None else start
    size = len(data)
    while pos + 4 <= size:
        frame = parse_header(data, pos)
        if frame is None:
            nxt = data.find(b"\xff", pos + 1)
            if nxt == -1:
                return
            pos = nxt
            continue
        yield frame
        pos += frame.length


def global_gain_positions(frame: Frame) -> list[int]:
    """Позиции (в битах от начала файла) всех полей global_gain кадра.

    Раскладка служебной части описана в ISO 11172-3 (MPEG1) и 13818-3 (MPEG2):
    у MPEG1 два гранула по 59 бит на канал, у MPEG2 — один гранул по 63 бита;
    global_gain идёт после part2_3_length (12 бит) и big_values (9 бит).
    """
    base = frame.side_info_offset * 8
    nch = frame.channels
    if frame.version == 3:  # MPEG1
        start = 9 + (5 if nch == 1 else 3) + 4 * nch
        return [base + start + i * 59 + 21 for i in range(2 * nch)]
    start = 8 + (1 if nch == 1 else 2)  # MPEG2 / MPEG2.5
    return [base + start + i * 63 + 21 for i in range(nch)]


# ---------------------------------------------------------------------------
# Применение усиления
# ---------------------------------------------------------------------------

def db_to_steps(gain_db: float) -> int:
    """Переводит желаемое усиление в целое число шагов по 1.5 дБ."""
    return int(round(gain_db / STEP_DB))


def apply_gain_steps(data: bytearray, steps: int) -> GainReport:
    """Прибавляет steps ко всем global_gain. Буфер меняется на месте."""
    report = GainReport(steps=steps)
    for frame in iter_frames(data):
        if report.frames == 0:
            report.sample_rate = frame.sample_rate
            report.bitrate_kbps = frame.bitrate_kbps
            report.channels = frame.channels
        report.frames += 1
        if steps == 0:
            continue
        for bit_pos in global_gain_positions(frame):
            old = _get_bits(data, bit_pos, 8)
            new = old + steps
            if new < 0:
                new, report.clamped = 0, report.clamped + 1
            elif new > 255:
                new, report.clamped = 255, report.clamped + 1
            if new != old:
                _set_bits(data, bit_pos, 8, new)
                report.changed += 1
    if report.frames == 0:
        raise Mp3Error("не найдено ни одного кадра MPEG Layer III")
    return report


def stream_info(data: bytes) -> Frame:
    """Первый корректный кадр — источник сведений о потоке."""
    for frame in iter_frames(data):
        return frame
    raise Mp3Error("не найдено ни одного кадра MPEG Layer III")
