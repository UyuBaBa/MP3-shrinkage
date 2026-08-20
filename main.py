"""Android-приложение: выравнивание громкости mp3 в выбранной папке.

Интерфейс на Kivy: выбор папки, целевой уровень, прогресс и журнал.
Вся работа идёт в отдельном потоке, интерфейс обновляется через @mainthread.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from kivy.app import App
from kivy.clock import mainthread
from kivy.core.text import LabelBase
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.textinput import TextInput
from kivy.utils import platform

import job
import probe

MODE_LABELS = {
    "Шкала mp3gain (89 дБ)": job.MODE_MP3GAIN,
    "Средний RMS (dBFS)": job.MODE_DBFS,
}
DEFAULT_TARGET = {job.MODE_MP3GAIN: "89", job.MODE_DBFS: "-18"}

#: Системные шрифты Android с японскими и китайскими иероглифами: в шрифте
#: Kivy по умолчанию их нет, и названия треков превратились бы в пустые рамки.
_CJK_FONTS = (
    "/system/fonts/NotoSansCJK-Regular.ttc",
    "/system/fonts/NotoSansJP-Regular.otf",
    "/system/fonts/NotoSansCJKjp-Regular.otf",
    "/system/fonts/DroidSansFallback.ttf",
    "/system/fonts/DroidSansFallbackFull.ttf",
)

MAX_LOG_LINES = 300


def _register_font() -> str:
    """Подключает шрифт с иероглифами, если он есть в системе."""
    for path in _CJK_FONTS:
        if os.path.exists(path):
            try:
                LabelBase.register(name="app", fn_regular=path)
                return "app"
            except Exception:
                continue
    return "Roboto"


def _start_dir() -> str:
    for candidate in ("/storage/emulated/0/Music", "/storage/emulated/0",
                      os.path.expanduser("~")):
        if os.path.isdir(candidate):
            return candidate
    return "/"


class FolderChooser(Popup):
    """Простой выбор папки: SAF не используем, работаем обычными путями."""

    def __init__(self, start_path: str, on_pick, **kwargs):
        self.on_pick = on_pick
        layout = BoxLayout(orientation="vertical", spacing=dp(6), padding=dp(6))

        shortcuts = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.chooser = FileChooserListView(path=start_path, dirselect=True,
                                           filters=[lambda folder, name: True])
        for title, path in (("Память", "/storage/emulated/0"),
                            ("Музыка", "/storage/emulated/0/Music"),
                            ("Загрузки", "/storage/emulated/0/Download")):
            if os.path.isdir(path):
                button = Button(text=title)
                button.bind(on_release=lambda _b, p=path: setattr(self.chooser, "path", p))
                shortcuts.add_widget(button)
        if shortcuts.children:
            layout.add_widget(shortcuts)
        layout.add_widget(self.chooser)

        buttons = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(6))
        cancel = Button(text="Отмена")
        cancel.bind(on_release=lambda *_: self.dismiss())
        choose = Button(text="Выбрать эту папку")
        choose.bind(on_release=self._choose)
        buttons.add_widget(cancel)
        buttons.add_widget(choose)
        layout.add_widget(buttons)

        super().__init__(title="Папка с mp3", content=layout,
                         size_hint=(0.95, 0.9), **kwargs)

    def _choose(self, *_args) -> None:
        selection = self.chooser.selection
        path = selection[0] if selection and os.path.isdir(selection[0]) \
            else self.chooser.path
        self.dismiss()
        self.on_pick(path)


class NormalizerApp(App):
    """Главное окно."""

    def build(self):
        self.title = "MP3 Уровень"
        self.font = _register_font()
        self.folder = _start_dir()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.log_lines: list[str] = []
        self.output_dir: Path | None = None

        root = BoxLayout(orientation="vertical", padding=dp(8), spacing=dp(6))
        root.add_widget(self._build_folder_row())
        root.add_widget(self._build_settings())
        root.add_widget(self._build_actions())
        root.add_widget(self._build_progress())
        root.add_widget(self._build_log())
        return root

    # ------------------------------------------------------------------ блоки
    def _label(self, text, **kwargs):
        kwargs.setdefault("font_name", self.font)
        return Label(text=text, **kwargs)

    def _build_folder_row(self):
        row = BoxLayout(size_hint_y=None, height=dp(76), spacing=dp(6))
        self.folder_label = self._label(self.folder, halign="left", valign="middle",
                                        shorten=True, shorten_from="left")
        self.folder_label.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        pick = Button(text="Выбрать\nпапку", size_hint_x=None, width=dp(110),
                      font_name=self.font)
        pick.bind(on_release=self._open_chooser)
        row.add_widget(self.folder_label)
        row.add_widget(pick)
        return row

    def _build_settings(self):
        box = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(190),
                        spacing=dp(6))

        mode_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        mode_row.add_widget(self._label("Шкала", size_hint_x=None, width=dp(110)))
        self.mode_spinner = Spinner(text=list(MODE_LABELS)[0],
                                    values=list(MODE_LABELS), font_name=self.font)
        self.mode_spinner.bind(text=self._on_mode_change)
        mode_row.add_widget(self.mode_spinner)
        box.add_widget(mode_row)

        target_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        target_row.add_widget(self._label("Уровень", size_hint_x=None, width=dp(110)))
        self.target_input = TextInput(text="89", multiline=False,
                                      font_name=self.font)
        self.target_input.bind(text=lambda *_: self._sync_output_name())
        target_row.add_widget(self.target_input)
        box.add_widget(target_row)

        out_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        out_row.add_widget(self._label("Новая папка", size_hint_x=None, width=dp(110)))
        self.output_input = TextInput(text="", multiline=False, font_name=self.font)
        out_row.add_widget(self.output_input)
        box.add_widget(out_row)

        clip_row = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(6))
        self.clip_check = CheckBox(active=True, size_hint_x=None, width=dp(48))
        clip_row.add_widget(self.clip_check)
        clip_label = self._label("Не допускать клиппинга", halign="left",
                                 valign="middle")
        clip_label.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        clip_row.add_widget(clip_label)
        box.add_widget(clip_row)
        self._sync_output_name()
        return box

    def _build_actions(self):
        row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(6))
        self.start_button = Button(text="Начать", font_name=self.font)
        self.start_button.bind(on_release=self._start)
        self.stop_button = Button(text="Стоп", disabled=True, font_name=self.font)
        self.stop_button.bind(on_release=self._stop)
        row.add_widget(self.start_button)
        row.add_widget(self.stop_button)
        return row

    def _build_progress(self):
        box = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(70),
                        spacing=dp(4))
        self.progress = ProgressBar(max=1, value=0, size_hint_y=None, height=dp(20))
        self.status_label = self._label("Готово к работе.", halign="left",
                                        valign="middle", size_hint_y=None,
                                        height=dp(44), shorten=True)
        self.status_label.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        box.add_widget(self.progress)
        box.add_widget(self.status_label)
        return box

    def _build_log(self):
        scroll = ScrollView()
        self.log_label = self._label("", size_hint_y=None, halign="left",
                                     valign="top", markup=False)
        self.log_label.bind(
            width=lambda w, *_: setattr(w, "text_size", (w.width, None)),
            texture_size=lambda w, size: setattr(w, "height", size[1]))
        scroll.add_widget(self.log_label)
        self.log_scroll = scroll
        return scroll

    # -------------------------------------------------------------- настройки
    def _current_mode(self) -> str:
        return MODE_LABELS[self.mode_spinner.text]

    def _on_mode_change(self, _spinner, text: str) -> None:
        mode = MODE_LABELS[text]
        self.target_input.text = DEFAULT_TARGET[mode]
        self._sync_output_name()

    def _settings(self) -> job.Settings:
        try:
            target = float(self.target_input.text.replace(",", "."))
        except ValueError:
            target = float(DEFAULT_TARGET[self._current_mode()])
        return job.Settings(target=target, mode=self._current_mode(),
                            avoid_clipping=bool(self.clip_check.active),
                            output_name=self.output_input.text.strip())

    def _sync_output_name(self) -> None:
        """Подставляет имя папки результата, пока пользователь не задал своё."""
        current = self.output_input.text.strip()
        if current and current != getattr(self, "_auto_output", ""):
            return
        settings = job.Settings(
            target=self._safe_target(), mode=self._current_mode())
        self._auto_output = settings.default_output_name()
        self.output_input.text = self._auto_output

    def _safe_target(self) -> float:
        try:
            return float(self.target_input.text.replace(",", "."))
        except ValueError:
            return float(DEFAULT_TARGET[self._current_mode()])

    def _open_chooser(self, *_args) -> None:
        FolderChooser(self.folder, self._set_folder).open()

    def _set_folder(self, path: str) -> None:
        self.folder = path
        self.folder_label.text = path

    # ------------------------------------------------------------------ запуск
    def _start(self, *_args) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not os.path.isdir(self.folder):
            self._set_status("Папка не найдена.")
            return
        if not self._ensure_permissions():
            return

        self.log_lines = []
        self.log_label.text = ""
        self.cancel_event.clear()
        self.start_button.disabled = True
        self.stop_button.disabled = False
        self.progress.value = 0
        self._set_status("Идёт анализ…")

        self.worker = threading.Thread(target=self._run, args=(self._settings(),),
                                       daemon=True)
        self.worker.start()

    def _run(self, settings: job.Settings) -> None:
        try:
            measurer = probe.make_probe()
            self._log(f"Измерение: {measurer.name}")
            summary = job.run_job(
                Path(self.folder), settings, measurer,
                log=self._log, progress=self._progress,
                is_cancelled=self.cancel_event.is_set,
            )
            self._finish(summary)
        except Exception as exc:  # noqa: BLE001 — показываем пользователю
            self._fail(f"{type(exc).__name__}: {exc}")

    def _stop(self, *_args) -> None:
        self.cancel_event.set()
        self.stop_button.disabled = True
        self._set_status("Останавливаюсь после текущего файла…")

    # ------------------------------------------------- обновления интерфейса
    @mainthread
    def _log(self, text: str) -> None:
        self.log_lines.append(text)
        if len(self.log_lines) > MAX_LOG_LINES:
            del self.log_lines[:-MAX_LOG_LINES]
        self.log_label.text = "\n".join(self.log_lines)
        self.log_scroll.scroll_y = 0

    @mainthread
    def _progress(self, current: int, total: int, name: str) -> None:
        self.progress.max = max(total, 1)
        self.progress.value = current
        if total:
            shown = min(current + 1, total)
            self.status_label.text = f"Файл {shown} из {total}: {name}" if name \
                else f"Обработано {current} из {total}"

    @mainthread
    def _set_status(self, text: str) -> None:
        self.status_label.text = text

    @mainthread
    def _finish(self, summary: job.Summary) -> None:
        self.start_button.disabled = False
        self.stop_button.disabled = True
        self.progress.value = self.progress.max
        parts = [f"обработано {summary.processed}"]
        if summary.copied:
            parts.append(f"без изменений {summary.copied}")
        if summary.limited:
            parts.append(f"урезано {summary.limited}")
        if summary.errors:
            parts.append(f"ошибок {summary.errors}")
        prefix = "Остановлено" if summary.cancelled else "Готово"
        self.status_label.text = f"{prefix}: {', '.join(parts)}"
        self._log("")
        self._log(f"{prefix}: {', '.join(parts)} (всего {summary.total})")
        if summary.total:
            self._log(f"Результат: {summary.output_dir}")

    @mainthread
    def _fail(self, text: str) -> None:
        self.start_button.disabled = False
        self.stop_button.disabled = True
        self.status_label.text = "Ошибка."
        self._log(f"ОШИБКА: {text}")

    # --------------------------------------------------------------- Android
    def _ensure_permissions(self) -> bool:
        """Запрашивает доступ к памяти; на Android 11+ — «Все файлы»."""
        if platform != "android":
            return True
        try:
            from android.permissions import Permission, request_permissions
            request_permissions([Permission.READ_EXTERNAL_STORAGE,
                                 Permission.WRITE_EXTERNAL_STORAGE])
            from jnius import autoclass
            version = autoclass("android.os.Build$VERSION")
            if version.SDK_INT >= 30:
                environment = autoclass("android.os.Environment")
                if not environment.isExternalStorageManager():
                    self._request_all_files_access(autoclass)
                    self._set_status("Разрешите доступ ко всем файлам и вернитесь.")
                    return False
        except Exception as exc:  # noqa: BLE001
            self._log(f"Не удалось запросить разрешения: {exc}")
        return True

    def _request_all_files_access(self, autoclass) -> None:
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        intent = autoclass("android.content.Intent")(
            autoclass("android.provider.Settings")
            .ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION)
        uri = autoclass("android.net.Uri").parse(
            "package:" + activity.getPackageName())
        intent.setData(uri)
        activity.startActivity(intent)

    def on_stop(self) -> None:
        self.cancel_event.set()


if __name__ == "__main__":
    NormalizerApp().run()
