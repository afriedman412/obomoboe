# PyInstaller build for the desktop app: `make app`, or see
# .github/workflows/release.yml. Produces dist/obomoboe.app on macOS and
# dist/obomoboe/obomoboe.exe on Windows -- Python and every dependency
# inside, nothing to install.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = [
    ("src/templates", "src/templates"),
    ("src/static", "src/static"),
]
# Stoplists, settings files and the public-suffix list these read from disk
# at runtime; PyInstaller only follows imports, so it would leave them out.
for package in ("trafilatura", "justext", "courlan", "htmldate", "tld",
                "lxml_html_clean", "babel", "tzlocal"):
    datas += collect_data_files(package)

# A "Developer ID Application: ..." identity from the keychain, set by the
# release workflow. Without one the app is only ad-hoc signed, which runs on
# the machine that built it but gets stopped by Gatekeeper anywhere else.
codesign_identity = os.environ.get("OBOMOBOE_CODESIGN_IDENTITY") or None

# One version number, in src/version.py; the app reads it at runtime too.
_ns: dict = {}
exec(open(os.path.join(SPECPATH, "src", "version.py")).read(), _ns)
version = _ns["__version__"]

# pywebview picks its platform backend at runtime, so name it here.
if sys.platform == "darwin":
    window_imports = ["webview.platforms.cocoa"]
elif sys.platform == "win32":
    window_imports = ["webview.platforms.winforms",
                      "webview.platforms.edgechromium", "clr"]
else:
    window_imports = []

a = Analysis(
    ["launcher.py"],
    datas=datas,
    # Imported inside create_app and the routes rather than at the top.
    hiddenimports=collect_submodules("src") + ["anthropic"] + window_imports,
    excludes=["tkinter", "pytest", "gunicorn", "PyQt5", "PyQt6", "PySide2",
              "PySide6", "gi", "cefpython3"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="obomoboe",
    # No terminal window: the app shows its own window instead.
    console=False,
    # Made from icons/source.png by scripts/make_icons.py.
    icon=os.path.join(SPECPATH, "icons", "obomoboe.ico"),
    upx=False,
    codesign_identity=codesign_identity,
)
coll = COLLECT(exe, a.binaries, a.datas, name="obomoboe", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="obomoboe.app",
        bundle_identifier="com.obomoboe.app",
        icon=os.path.join(SPECPATH, "icons", "obomoboe.icns"),
        codesign_identity=codesign_identity,
        info_plist={
            "CFBundleDisplayName": "obomoboe",
            "CFBundleShortVersionString": version,
            "NSHighResolutionCapable": True,
        },
    )
