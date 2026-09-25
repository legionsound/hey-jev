"""Build the app bundle in alias mode, it runs the code straight from this folder: python setup.py py2app -A"""
from setuptools import setup

APP_NAME = "Hey Jev"

setup(
    name=APP_NAME,
    app=["app.py"],
    options={"py2app": {
        "argv_emulation": False,
        "iconfile": "assets/icon.icns",
        # Function-level imports py2app's static scan can miss: screen.py
        # imports these lazily (AX control via ApplicationServices,
        # capture/clicks via Quartz, identity via AppKit/Foundation).
        "includes": ["Quartz", "ApplicationServices", "AppKit", "Foundation"],
        "plist": {
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": APP_NAME,
            "CFBundleIdentifier": "com.heyjev.app",
            "CFBundleShortVersionString": "0.3.0",
            "LSUIElement": False,
            "NSHighResolutionCapable": True,
            "NSMicrophoneUsageDescription": "Hey Jev listens for your commands.",
            "NSSpeechRecognitionUsageDescription": "Hey Jev can turn your voice into text on this Mac with Apple dictation.",
            "NSAppleEventsUsageDescription": "Hey Jev controls Spotify, Safari, Chrome, System Events, volume and dark mode for you.",
        },
    }},
    setup_requires=["py2app"],
)
