[app]

title = MP3 Уровень
package.name = mp3level
package.domain = ru.local

source.dir = .
source.include_exts = py
version = 1.0

# android — доступ к разрешениям, numpy — быстрый подсчёт RMS из PCM
requirements = python3,kivy==2.3.0,numpy,android

orientation = portrait
fullscreen = 0

# MANAGE_EXTERNAL_STORAGE нужен на Android 11+, чтобы работать с обычными
# путями (/storage/emulated/0/...) вместо SAF; выдаётся вручную в настройках.
android.permissions = android.permission.READ_EXTERNAL_STORAGE,android.permission.WRITE_EXTERNAL_STORAGE,android.permission.MANAGE_EXTERNAL_STORAGE,android.permission.READ_MEDIA_AUDIO

android.api = 34
android.minapi = 24
android.ndk_api = 24

# arm64-v8a — все телефоны последних лет. Добавьте armeabi-v7a, если нужен
# совсем старый аппарат: сборка станет примерно вдвое дольше.
android.archs = arm64-v8a

android.accept_sdk_license = True
android.allow_backup = True

# экран не гаснет, пока идёт обработка папки
android.wakelock = True

p4a.bootstrap = sdl2

[buildozer]

log_level = 2
warn_on_root = 0
