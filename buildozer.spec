[app]
# IFTTW = If This Then Webhook
title = IFTTW
package.name = ifttw
package.domain = org.ifttw
source.dir = .
source.include_exts = py,html,png,woff,txt
source.exclude_dirs = .github,bin,.buildozer,assets,android,p4a
version = 1.0
requirements = python3,certifi,pyjnius
orientation = portrait
fullscreen = 0
icon.filename = %(source.dir)s/assets/icon.png

# Adaptive launcher icon: a transparent logo (foreground) on a plain black background.
# Android 12+ also uses it for the splash screen, so no white box appears behind the logo.
icon.adaptive_foreground.filename = %(source.dir)s/assets/icon_fg.png
icon.adaptive_background.filename = %(source.dir)s/assets/icon_bg.png

# Loading screen: plain black with the logo only.
presplash.filename = %(source.dir)s/assets/presplash.png
android.presplash_color = #000000

# Java receivers for real-time charger events and notification buttons (android/java/org/ifttw/)
android.add_src = android/java

# Build hook: adds BootReceiver to AndroidManifest.xml so the bot service starts by itself after the phone boots.
p4a.hook = p4a/hook.py

# The app screen is a WebView that opens the local server on port 5000.
p4a.bootstrap = webview
p4a.port = 5000

# Stable python-for-android release that matches NDK 25b.
p4a.branch = v2024.01.21

# The bot is a service that app.py promotes to the foreground (show_service_notification -> ServiceNotifier.java).
# Its persistent, Termux-style notification reads "Running" plus the number of active Applets (e.g. 4 Applets)
# and has an Exit button. The only other notification is "Sent to webhook", shown after a successful webhook.
# Exit, or turning off the last Applet, closes everything (screen, service and all processes) and needs no extra permission.
# There is no wake lock: the charger is tracked through system broadcasts, so the CPU does not have to stay awake
# (the WAKE_LOCK permission is not requested).
services = Bot:ifttw/service.py:sticky

# INTERNET: NASA and webhooks | FOREGROUND_SERVICE + POST_NOTIFICATIONS: "Running" notification |
# REQUEST_IGNORE_BATTERY_OPTIMIZATIONS: keeps the system from killing the service | RECEIVE_BOOT_COMPLETED: start at boot
android.permissions = INTERNET,FOREGROUND_SERVICE,POST_NOTIFICATIONS,REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,RECEIVE_BOOT_COMPLETED
android.api = 33
android.minapi = 24
android.ndk = 25b
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
