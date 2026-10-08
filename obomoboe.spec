# PyInstaller build for the desktop app: `make app`, or see
# .github/workflows/release.yml. Produces dist/obomoboe.app on macOS and
# dist/obomoboe/obomoboe.exe on Windows -- Python and every dependency
# inside, nothing to install.
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

a = Analysis(
    ["launcher.py"],
    datas=datas,
    # Imported inside create_app and the routes rather than at the top.
    hiddenimports=collect_submodules("src") + ["anthropic"],
    excludes=["tkinter", "pytest", "gunicorn"],
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="obomoboe",
    # No terminal window: the launcher opens the browser and exits, and the
    # server runs in the background until you press quit.
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="obomoboe", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="obomoboe.app",
        bundle_identifier="com.obomoboe.app",
        info_plist={
            "CFBundleDisplayName": "obomoboe",
            "CFBundleShortVersionString": "1.0.0",
            "NSHighResolutionCapable": True,
        },
    )
