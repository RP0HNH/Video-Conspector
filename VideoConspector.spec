# -*- mode: python ; coding: utf-8 -*-
"""Сборка Video Conspector (onedir). Запуск: build_exe.ps1.

Правило: пакета нет в коде проекта — его нет и в exe.
Этот файл написан вручную; не запускайте PyInstaller с флагами вместо него,
иначе он будет перезаписан автогенерированным.
"""
from PyInstaller.utils.hooks import collect_all

# Пакеты, которые реально используются в рантайме и грузят данные/DLL динамически
RUNTIME_PACKAGES = [
    "faster_whisper",   # assets, токенизатор
    "ctranslate2",      # нативные DLL
    "av",               # ffmpeg-библиотеки PyAV
    "tokenizers",
    "huggingface_hub",  # скачивание модели Whisper
    "yt_dlp",           # экстракторы подгружаются динамически
    "pytesseract",
]

# Не используются проектом; часть из них может тянуться из окружения сборки
EXCLUDES = [
    "onnxruntime",      # нужен только VAD (по умолчанию выключен)
    "torch", "torchvision", "torchaudio", "tensorflow",
    "matplotlib", "scipy", "pandas", "sklearn", "numba", "sympy",
    "IPython", "jedi", "notebook", "jupyter", "jupyter_client", "ipykernel",
    "sphinx", "docutils", "babel", "pygments",
    "cv2", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "pytest",
]

datas, binaries, hiddenimports = [], [], ["pkg_resources"]
for package in RUNTIME_PACKAGES:
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["gui.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VideoConspector",
    console=False,
    upx=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="VideoConspector",
)
