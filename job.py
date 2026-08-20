"""Обход папки и приведение всех mp3 к заданному уровню.

Логика общая для телефона и компьютера: интерфейс (Kivy) только показывает
то, что сюда передаётся через колбэки, поэтому её можно гонять тестами.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import mp3gain

#: Шкала mp3gain: 89 дБ примерно соответствует -18 dBFS среднего RMS.
MP3GAIN_SPL_OFFSET_DB = 107.0

MODE_MP3GAIN = "mp3gain"   # цель вида 89 дБ
MODE_DBFS = "dbfs"         # цель вида -18 dBFS

#: Максимально допустимый пик после усиления при защите от клиппинга.
PEAK_CEILING_DB = -0.3

#: Метка в папке результата: по ней следующий запуск её пропускает.
OUTPUT_MARKER = ".mp3_normalizer_output"


@dataclass
class Settings:
    """Параметры обработки, задаются в интерфейсе."""

    target: float = 89.0
    mode: str = MODE_MP3GAIN
    avoid_clipping: bool = True
    output_name: str = ""

    def target_dbfs(self) -> float:
        if self.mode == MODE_MP3GAIN:
            return self.target - MP3GAIN_SPL_OFFSET_DB
        return self.target

    def unit(self) -> str:
        return "дБ" if self.mode == MODE_MP3GAIN else "dBFS"

    def to_display(self, dbfs: float) -> float:
        """Переводит dBFS в единицы, выбранные пользователем."""
        if self.mode == MODE_MP3GAIN:
            return dbfs + MP3GAIN_SPL_OFFSET_DB
        return dbfs

    def default_output_name(self) -> str:
        suffix = "dB" if self.mode == MODE_MP3GAIN else "dBFS"
        return f"normalized_{self.target:g}{suffix}"


@dataclass
class FileResult:
    rel_path: str
    status: str                     # ok | copied | limited | error
    measured: float | None = None   # в единицах пользователя
    achieved: float | None = None
    steps: int = 0
    message: str = ""


@dataclass
class Summary:
    output_dir: Path
    total: int = 0
    processed: int = 0
    copied: int = 0
    limited: int = 0
    errors: int = 0
    cancelled: bool = False
    results: list[FileResult] = field(default_factory=list)


def find_mp3_files(root: Path, exclude: Path | None = None,
                   on_skip: Callable[[Path], None] | None = None) -> list[Path]:
    """Рекурсивно ищет mp3, пропуская папки с результатами прошлых запусков."""
    files: list[Path] = []
    root_resolved = Path(root).resolve()
    exclude_resolved = exclude.resolve() if exclude else None
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath).resolve()
        if exclude_resolved and (current == exclude_resolved
                                 or exclude_resolved in current.parents):
            dirnames[:] = []
            continue
        if current != root_resolved and OUTPUT_MARKER in filenames:
            dirnames[:] = []
            if on_skip:
                on_skip(Path(dirpath))
            continue
        for name in sorted(filenames):
            if name.lower().endswith(".mp3"):
                files.append(Path(dirpath) / name)
    return sorted(files)


def unique_dir(parent: Path, name: str) -> Path:
    """Несуществующий путь parent/name, при конфликте с номером."""
    candidate = parent / name
    counter = 2
    while candidate.exists():
        candidate = parent / f"{name}_{counter}"
        counter += 1
    return candidate


def plan_gain(measurement, settings: Settings) -> tuple[int, bool]:
    """Считает сдвиг в шагах по 1.5 дБ и признак урезания по пику."""
    gain_db = settings.target_dbfs() - measurement.rms_dbfs
    limited = False
    if settings.avoid_clipping and gain_db > 0:
        headroom = PEAK_CEILING_DB - measurement.peak_dbfs
        if gain_db > headroom:
            gain_db = max(headroom, 0.0)
            limited = True
    return mp3gain.db_to_steps(gain_db), limited


def run_job(
    src_root: Path,
    settings: Settings,
    probe,
    *,
    output_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> Summary:
    """Приводит все mp3 из src_root к нужному уровню, складывая копии в новую папку."""
    src_root = Path(src_root)
    if not src_root.is_dir():
        raise NotADirectoryError(f"Папка не найдена: {src_root}")

    out_root = Path(output_dir) if output_dir else unique_dir(
        src_root, settings.output_name.strip() or settings.default_output_name())

    def emit(text: str) -> None:
        if log:
            log(text)

    skipped: list[Path] = []
    files = find_mp3_files(src_root, exclude=out_root, on_skip=skipped.append)
    summary = Summary(output_dir=out_root, total=len(files))
    for folder in skipped:
        emit(f"Пропущен прошлый результат: {folder.relative_to(src_root)}")
    if not files:
        emit("mp3-файлы не найдены.")
        return summary

    unit = settings.unit()
    emit(f"Найдено файлов: {len(files)}")
    emit(f"Папка результата: {out_root}")
    out_root.mkdir(parents=True, exist_ok=True)
    _write_marker(out_root, settings)

    for index, src in enumerate(files):
        rel = src.relative_to(src_root)
        if progress:
            progress(index, len(files), str(rel))
        if is_cancelled and is_cancelled():
            summary.cancelled = True
            emit("Остановлено.")
            break

        dst = out_root / rel
        try:
            _process_one(src, dst, settings, probe, summary, rel, unit, emit)
        except Exception as exc:  # noqa: BLE001 — файл не должен ронять весь прогон
            summary.errors += 1
            summary.results.append(FileResult(str(rel), "error", message=str(exc)))
            emit(f"! {rel} — ошибка: {exc}")

    if progress:
        progress(summary.processed + summary.copied + summary.errors,
                 summary.total, "")
    return summary


def _process_one(src: Path, dst: Path, settings: Settings, probe,
                 summary: Summary, rel: Path, unit: str,
                 emit: Callable[[str], None]) -> None:
    measurement = probe.measure(src)
    steps, limited = plan_gain(measurement, settings)
    measured = settings.to_display(measurement.rms_dbfs)
    achieved = measured + steps * mp3gain.STEP_DB

    dst.parent.mkdir(parents=True, exist_ok=True)
    if steps == 0:
        shutil.copy2(src, dst)
        summary.copied += 1
        summary.results.append(FileResult(str(rel), "copied", measured, measured))
        emit(f"= {rel}  ({measured:.1f} {unit}) — уже на уровне, копия")
        return

    data = bytearray(src.read_bytes())
    report = mp3gain.apply_gain_steps(data, steps)
    dst.write_bytes(data)

    status = "limited" if limited else "ok"
    summary.processed += 1
    if limited:
        summary.limited += 1
    notes = []
    if limited:
        notes.append("урезано, защита от клиппинга")
    if report.clamped:
        notes.append(f"{report.clamped} полей упёрлись в предел")
    summary.results.append(FileResult(str(rel), status, measured, achieved, steps,
                                      "; ".join(notes)))
    tail = f"  ({'; '.join(notes)})" if notes else ""
    emit(f"+ {rel}  {measured:.1f} -> {achieved:.1f} {unit} "
         f"({steps * mp3gain.STEP_DB:+.1f} дБ){tail}")


def _write_marker(out_root: Path, settings: Settings) -> None:
    try:
        (out_root / OUTPUT_MARKER).write_text(
            f"mp3_normalizer: {settings.target:g} ({settings.mode})\n",
            encoding="utf-8")
    except OSError:
        pass  # метка необязательна
