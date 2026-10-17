"""IFTTW entry point.

Android:    starts the bot service (local HTTP server + monitoring) and then idles. The app screen (a WebView)
            opens http://127.0.0.1:5000, which that service serves.
Termux/PC:  runs the application directly.

All application code lives in the ifttw/ package; this is deliberately the only script outside it.
"""
import os
import socket
import threading
import time

PORT = 5000
NOTIF_PERMISSION = 'android.permission.POST_NOTIFICATIONS'

_service_handles = None      # (activity, ServiceBot class)


def service_handles():
    """Return (activity, ServiceBot class).

    The first call must come from the main thread: app classes (org.kivy.android.*) cannot be loaded from
    threads created by Python.
    """
    global _service_handles
    if _service_handles is None:
        from jnius import autoclass
        activity = autoclass('org.kivy.android.PythonActivity').mActivity
        _service_handles = (activity, autoclass(activity.getPackageName() + '.ServiceBot'))
    return _service_handles


def start_service():
    activity, service_class = service_handles()
    service_class.start(activity, '')


def service_alive():
    try:
        socket.create_connection(('127.0.0.1', PORT), 1).close()
        return True
    except OSError:
        return False


def poke_service(cmd):
    """Send a command to the service through its local server, e.g. 'repost' to show the notification again."""
    try:
        import json
        import urllib.request                          # imported on demand: the screen process rarely needs it
        req = urllib.request.Request(f'http://127.0.0.1:{PORT}/api/ctl', data=json.dumps({'cmd': cmd}).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=3).read()
        return True
    except Exception as e:
        print('poke service:', e)
        return False


def watch_notif_permission(activity, granted):
    """Android 13+: the notification cannot appear until the permission is granted, and the system does not tell
    us when that happens. Poll for up to 3 minutes (only while the permission is missing), then ask the service
    to show the notification."""
    for _ in range(90):
        time.sleep(2)
        try:
            ok = activity.checkSelfPermission(NOTIF_PERMISSION) == granted
        except BaseException:
            return
        if ok:
            poke_service('repost')
            return


def keep_service():
    """The screen process has no recurring work, so it just waits without ever waking up.

    The charger is handled by the service through a system receiver, the Exit button closes the screen from the
    service, and the service is started once every time the app is opened (see android_setup).
    """
    threading.Event().wait()


def request_notification_permission(activity):
    """Android 13+: ask for POST_NOTIFICATIONS so the persistent and "Sent to webhook" notifications can appear."""
    from jnius import autoclass
    if autoclass('android.os.Build$VERSION').SDK_INT < 33:
        return
    granted = autoclass('android.content.pm.PackageManager').PERMISSION_GRANTED
    if activity.checkSelfPermission(NOTIF_PERMISSION) != granted:
        activity.requestPermissions([NOTIF_PERMISSION], 1)
        threading.Thread(target=watch_notif_permission, args=(activity, granted), daemon=True).start()


def request_battery_exemption(activity):
    """Ask once for the battery optimization exemption, so Android does not kill the service."""
    from jnius import autoclass
    flag = os.path.join(os.environ.get('ANDROID_PRIVATE') or '.', 'battery_asked')
    if os.path.exists(flag):
        return
    Context = autoclass('android.content.Context')
    Intent = autoclass('android.content.Intent')
    Settings = autoclass('android.provider.Settings')
    Uri = autoclass('android.net.Uri')
    pkg = activity.getPackageName()
    if not activity.getSystemService(Context.POWER_SERVICE).isIgnoringBatteryOptimizations(pkg):
        intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
        intent.setData(Uri.parse('package:' + pkg))
        activity.startActivity(intent)
    open(flag, 'w').write('1')


def android_setup():
    activity, _ = service_handles()

    # Start the bot service. If it is already running, do NOT call startService again: python-for-android answers
    # "service exists" with START_NOT_STICKY, which can strip the service's sticky behavior. Only ask it to post
    # its persistent notification again.
    if service_alive():
        poke_service('repost')
    else:
        start_service()

    for step in (request_notification_permission, request_battery_exemption):
        try:
            step(activity)
        except Exception as e:
            print(f'{step.__name__}:', e)


if 'ANDROID_ARGUMENT' in os.environ:
    try:
        android_setup()
    except BaseException:
        import traceback
        from ifttw import errpage
        errpage.show('main', traceback.format_exc())   # show the cause on screen
    keep_service()
else:
    from ifttw import app
    app.run()
