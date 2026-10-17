#!/usr/bin/env python3
"""IFTTW (If This Then Webhook): local HTTP server and monitoring bot.

Termux/PC: run ``python main.py`` and open http://127.0.0.1:5000.
Android:   started as a foreground service by ifttw/service.py.

While the service runs it shows a persistent "Running · N Applets" notification with an Exit button, and a
short "Sent to webhook" notification after every successful webhook. The app shuts down completely (screen,
service and every process) when Exit is tapped or the last Applet is disconnected; see do_exit().

Charger events arrive in real time through android/java/org/ifttw/PlugReceiver.java on Android and through
polling on Termux/PC. NASA feeds are only fetched when "Check now" is tapped.
"""
import html
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Rarely used modules (subprocess, xml.etree, email.utils) are imported on demand. The service stays resident
# in the background, so every module loaded at startup would permanently occupy RAM.

# ---------------------------------------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------------------------------------

DIR = os.path.dirname(os.path.abspath(__file__))       # the ifttw/ package folder
ROOT = os.path.dirname(DIR)                            # project root (contains main.py)
WEB_DIR = os.path.join(DIR, 'web')
RES_DIR = os.path.join(DIR, 'res')
PORT = 5000
IS_ANDROID = 'ANDROID_ARGUMENT' in os.environ
DATA_DIR = os.environ.get('ANDROID_PRIVATE') or ROOT   # app-private storage on Android
DB = os.path.join(DATA_DIR, 'data.json')
IDLE_FLAG = os.path.join(DATA_DIR, 'all_off')          # exists while every Applet is off (read by BootReceiver.java)

# NASA sources. APOD moved to science.nasa.gov on 29 Sep 2026; api.nasa.gov/planetary/apod now returns only a logo.
APOD_API = 'https://science.nasa.gov/wp-json/wp/v2/apod-basic'
IOTD_FEED = 'https://www.nasa.gov/feeds/iotd-feed/'
IOTD_FEEDS = (IOTD_FEED, 'https://www.nasa.gov/rss/dyn/lg_image_of_the_day.rss')   # the legacy URL redirects to the new feed
NASA_HEADERS = {  # sent to NASA only; Discord webhooks use the default User-Agent (UA)
    'User-Agent': 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/126.0.0.0 Mobile Safari/537.36',
    'Accept': 'application/json, application/rss+xml, application/xml;q=0.9, */*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}
UA = {'User-Agent': 'Mozilla/5.0 (Linux; Android 14) ifttw'}

APOD_MAX = 10                                          # newest APOD entries to fetch
IOTD_MAX = 10                                          # newest Image of the Day entries to fetch
SHOW_ACTIVITY = 10                                     # entries shown in View activity, per Applet
KEEP_PER_APPLET = max(SHOW_ACTIVITY, APOD_MAX, IOTD_MAX)   # older entries are pruned (NASA feeds only return 10)
APOD_TIMEOUT = 45                                      # the APOD API is slow, so it gets a longer timeout

BAT_EVERY = 10                                         # charger polling interval (s) on Termux/PC
BAT_EVERY_ANDROID = 30                                 # fallback interval (s) if the Android receiver fails to start

IOTD, APOD, PLUG, UNPLUG = 'cQ5FmX9v', 'wT7nR2Kb', 'pB4vH8Ls', 'xJ9dM3Qz'
APPLET_NAMES = {IOTD: 'Image of the day by NASA', APOD: 'Astronomy picture of the day by NASA',
                PLUG: 'Charger plugged', UNPLUG: 'Charger unplugged'}
KEYS = ('on', 'u', 'h', 'b', 'm', 'ct', 'title', 'auto')   # persisted Applet settings; auto: 1 = Automation, 0 = Manual

# Android notifications. A channel's importance cannot change after creation, so a new importance needs a new
# channel id and the old ones are deleted (see cleanup_old_notification).
SVC_ID, SVC_CHANNEL = 7002, 'ifttw_running_min'        # persistent "Running" notification; must match ServiceNotifier.java
SENT_ID, SENT_CHANNEL = 7010, 'ifttw_sent_min'         # a single "Sent to webhook" notification; a new one replaces the old
NOTIF_ID, NOTIF_CHANNEL = 7001, 'ifttw_active'         # legacy "N Applets active" notification (only cleaned up)
OLD_CHANNELS = ('ifttw_service', 'ifttw_running', 'ifttw_sent', 'ifttw_sent_low')
ACTION_EXIT = 'org.ifttw.action.EXIT'                  # must match PlugReceiver.java
ACTION_REPOST = 'org.ifttw.action.REPOST'              # the notification was swiped away (Android 14+); post it again
FLAG_NO_CLEAR = 32                                     # Notification.FLAG_NO_CLEAR
FOREGROUND_SERVICE_IMMEDIATE = 1                       # Notification.FOREGROUND_SERVICE_IMMEDIATE
PRIORITY_MIN = -2                                      # Notification.PRIORITY_MIN
IMPORTANCE_VISIBLE = 200                               # RunningAppProcessInfo.IMPORTANCE_VISIBLE

EXIT_DELAY = 1.5                                       # delay (s) before closing so the toggle animation can finish
IDLE_CHECK = 15                                        # interval (s) between "is the screen still in use?" checks

# Classes loaded on the service's main thread: app classes cannot be loaded from threads created by Python.
JAVA_PRELOAD = (
    'android.content.Intent', 'android.content.IntentFilter', 'android.content.Context',
    'android.app.Notification$Builder', 'android.app.Notification$Action$Builder',
    'android.app.NotificationChannel', 'android.app.NotificationManager', 'android.app.PendingIntent',
    'android.app.ActivityManager', 'android.os.Build$VERSION', 'android.os.Process',
    'android.graphics.drawable.Icon', 'android.graphics.BitmapFactory',
)

# ---------------------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------------------

lock = threading.RLock()
S = {'applets': {}, 'activity': [], 'next': 1}         # persisted to data.json
_dirty = False                                         # unsaved changes exist (lets one Check now write once)

PLUG_EVENTS = False                                    # True: Android reports charger events, so no polling
_last_plug_event = (None, 0.0)                         # last charger event, used to drop duplicate broadcasts
_foreground = False                                    # the service already runs in the foreground
_notifier = None                                       # Java class org.ifttw.ServiceNotifier (see load_notifier)
_closing = False                                       # do_exit() is already running
_exit_timer = None                                     # pending shutdown after the last Applet was turned off
_ssl_ctx = None
_android_ctx = None
NOTIF_LOG = []                                         # recent notification events, served by GET /api/diag
CHECKS = {}                                            # latest "Check now" result per Applet


def load_state():
    """Load data.json into S; a corrupt file is kept as data.json.bad and the app starts with empty data."""
    if not os.path.exists(DB):
        return
    try:
        with open(DB, encoding='utf-8') as f:
            S.update(json.load(f))
    except (OSError, ValueError) as e:                 # e.g. the phone lost power while saving
        print('data.json is corrupt, moved to data.json.bad:', e)
        try:
            os.replace(DB, DB + '.bad')
        except OSError:
            pass


def save():
    global _dirty
    with lock:
        with open(DB + '.tmp', 'w', encoding='utf-8') as f:
            json.dump(S, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(DB + '.tmp', DB)
        _dirty = False


def meta_of(aid):
    """Per-Applet metadata: 'since' (when it was connected) and 'last' (latest activity)."""
    return S.setdefault('meta', {}).setdefault(aid, {})


def migrate():
    """Fill in dates that older data files lack, so the Activity text is never empty."""
    now = time.time()
    for aid, applet in S['applets'].items():
        meta = meta_of(aid)
        if applet.get('on', 1) and 'since' not in meta:
            meta['since'] = now
        if 'last' not in meta:
            times = [x.get('posted') or x['ts'] for x in S['activity'] if x['aid'] == aid]
            if times:
                meta['last'] = max(times)


load_state()
migrate()


def is_on(aid):
    """Applets that were never saved count as connected."""
    return S['applets'].get(aid, {}).get('on', 1)


def is_auto(aid):
    return S['applets'].get(aid, {}).get('auto', 0)


def active_count():
    return sum(1 for aid in APPLET_NAMES if is_on(aid))


def any_active():
    return active_count() > 0


def running_title():
    """Title of the persistent notification, e.g. "Running  ·  4 Applets"."""
    n = active_count()
    return f"Running  \u00b7  {n} Applet{'' if n == 1 else 's'}"


def sync_idle_flag():
    """Android: keep the 'all_off' marker file in sync, so BootReceiver.java skips starting the service at boot."""
    if not IS_ANDROID:
        return
    try:
        if any_active():
            if os.path.exists(IDLE_FLAG):
                os.remove(IDLE_FLAG)
        else:
            open(IDLE_FLAG, 'w').close()
    except OSError as e:
        print('all_off marker:', e)


def nlog(msg):
    """Record a notification event (last 30 lines, visible at /api/diag)."""
    print('notification:', msg)
    NOTIF_LOG.append(time.strftime('%H:%M:%S ') + str(msg))
    del NOTIF_LOG[:-30]


# ---------------------------------------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------------------------------------

def ssl_ctx():
    """SSL context backed by certifi, or None to use the system CAs.

    Android/p4a has no CA bundle of its own. The context is built on first HTTPS use rather than at service
    start, because loading the bundle costs CPU and RAM and many sessions never send a webhook.
    """
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ssl_ctx = False                           # no certifi (Termux/PC): use the system CAs
    return _ssl_ctx or None


def http_request(url, data=None, headers=None, method=None, timeout=20):
    req = urllib.request.Request(url, data=data, method=method, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx()) as r:
        return r.status, r.read()


def error_reason(e):
    """Human-readable failure reason, shown on the Activity screen and not only in the log."""
    if isinstance(e, urllib.error.HTTPError):
        return f'HTTP {e.code} {e.reason}'.strip()
    reason = getattr(e, 'reason', None) or e
    if isinstance(reason, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(reason):
        return 'SSL certificate error - check that the phone date and time are correct'
    if isinstance(reason, (socket.timeout, TimeoutError)) or 'timed out' in str(reason):
        return 'Connection timed out - check the internet connection'
    if isinstance(e, urllib.error.URLError):
        return f'Cannot reach NASA ({reason})'
    return str(e) or e.__class__.__name__


def nasa_get(urls, timeout, tries=2):
    """GET from NASA with fallback URLs.

    Each URL is retried on a timeout or a 5xx error. A 4xx response moves straight to the next URL. Network
    failures (timeout, DNS, SSL) abort the loop, because every URL points to the same host.
    """
    last = None
    for url in urls:
        for attempt in range(tries):
            try:
                return http_request(url, headers=NASA_HEADERS, timeout=timeout)[1]
            except urllib.error.HTTPError as e:
                last = e
                if e.code < 500:
                    break
            except Exception as e:
                last = e
            if attempt < tries - 1:
                time.sleep(3)
        if last is not None and not isinstance(last, urllib.error.HTTPError):
            break
    raise last


# ---------------------------------------------------------------------------------------------------------
# Android helpers
# ---------------------------------------------------------------------------------------------------------

def jclass(name):
    """Load a Java class through pyjnius (imported on demand, since it only exists on Android)."""
    from jnius import autoclass
    return autoclass(name)


def sdk_int():
    return jclass('android.os.Build$VERSION').SDK_INT


def notification_manager(ctx):
    return ctx.getSystemService(jclass('android.content.Context').NOTIFICATION_SERVICE)


def activity_manager(ctx):
    return ctx.getSystemService(jclass('android.content.Context').ACTIVITY_SERVICE)


def android_context():
    """Android Context, resolved once on the service's main thread and cached.

    Threads created by Python cannot load app classes (org.kivy.android.*): they fail with 'Class not found'.
    """
    global _android_ctx
    if _android_ctx is not None:
        return _android_ctx
    ctx = None
    try:
        ctx = jclass('org.kivy.android.PythonService').mService      # when running inside the service
    except Exception:
        pass
    if ctx is None:
        ctx = jclass('org.kivy.android.PythonActivity').mActivity
    _android_ctx = ctx
    return ctx


# ---------------------------------------------------------------------------------------------------------
# Activity log and NASA feeds
# ---------------------------------------------------------------------------------------------------------

def add(aid, key, vals, title, sub, ts, persist=True):
    """Add an Activity entry and return 1 if it is new, else 0.

    An existing entry with the same (aid, key) is only refreshed when its image changed.
    With persist=False the caller must call save() once after its batch of additions.
    """
    global _dirty
    with lock:
        img = vals.get('ImageSourceURL', '')
        for entry in S['activity']:
            if entry['aid'] == aid and entry['key'] == key:
                if entry.get('img') != img:
                    entry['img'], entry['vals'] = img, vals
                    _dirty = True
                    if persist:
                        save()
                return 0
        S['activity'].append({'id': S['next'], 'aid': aid, 'key': key, 'ts': ts, 'title': title, 'sub': sub,
                              'img': img, 'vals': vals, 'posted': 0})
        S['next'] += 1
        meta_of(aid)['last'] = time.time()
        mine = [x for x in S['activity'] if x['aid'] == aid]
        if len(mine) > KEEP_PER_APPLET:
            oldest = sorted(mine, key=lambda x: x['ts'])[:len(mine) - KEEP_PER_APPLET]
            drop = {x['id'] for x in oldest}
            S['activity'] = [x for x in S['activity'] if x['id'] not in drop]
        _dirty = True
        if persist:
            save()
        return 1


def iotd_time(item):
    pub = item.findtext('pubDate')
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(pub) if pub else datetime.now()
    except (TypeError, ValueError):
        return datetime.now()


def poll_iotd():
    """Fetch the newest Image of the Day entries; return (new, found)."""
    import xml.etree.ElementTree as ET
    drop_imageless_entries()
    raw = nasa_get(IOTD_FEEDS, 20)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        root = None
    if root is None or (root.tag != 'rss' and root.find('channel') is None):       # e.g. a WiFi login page (HTML)
        raise RuntimeError('NASA sent a page that is not an RSS feed (captive portal, VPN or firewall?)')
    items = [(iotd_time(it), it) for it in root.iter('item')]
    if not items:
        raise RuntimeError('The NASA feed is empty')
    items.sort(key=lambda row: row[0].timestamp(), reverse=True)
    new = found = 0
    for dt, it in items[:IOTD_MAX]:
        title = (it.findtext('title') or '').strip()
        link = (it.findtext('link') or '').strip()
        desc = it.findtext('description') or ''
        enclosure = it.find('enclosure')
        src = enclosure.get('url') if enclosure is not None else ''
        if not src:
            continue                                   # no photo: not listed, not sent to the webhook
        vals = {'ImageTitle': title, 'ImageSourceURL': src,
                'ExplanationHTML': desc, 'Explanation': html.unescape(re.sub('<[^>]+>', '', desc)).strip(),
                'GalleryURL': link, 'PublishedDate': dt.strftime('%B %d, %Y')}
        new += add(IOTD, link or title, vals, title, vals['PublishedDate'], dt.timestamp(), persist=False)
        found += 1
    if _dirty:
        save()
    return new, found


def apod_text(s):
    s = re.sub(r'<br\s*/?>', '\n', s or '')
    s = html.unescape(re.sub('<[^>]+>', '', s)).strip()
    return re.sub(r'^Explanation:\s*', '', s)


def drop_fake_apod_entries():
    """Remove bogus entries (the NASA logo) that the old APOD API left in the Activity log."""
    with lock:
        before = len(S['activity'])
        S['activity'] = [x for x in S['activity']
                         if not (x['aid'] == APOD and 'nasa-logo' in (x.get('img') or ''))]
        if len(S['activity']) != before:
            save()


def drop_imageless_entries():
    """Remove NASA Activity entries that have no photo (listed by an earlier version)."""
    with lock:
        before = len(S['activity'])
        S['activity'] = [x for x in S['activity'] if x['aid'] not in (IOTD, APOD) or x.get('img')]
        if len(S['activity']) != before:
            save()


def poll_apod():
    """Fetch the newest Astronomy Pictures of the Day; return (new, found)."""
    drop_fake_apod_entries()
    drop_imageless_entries()
    urls = (f'{APOD_API}?per_page={APOD_MAX}&_fields=date,title,permalink,media_type,explanation,hdurl',
            f'{APOD_API}?per_page={APOD_MAX}')         # fallback in case NASA rejects the _fields parameter
    raw = nasa_get(urls, APOD_TIMEOUT)
    try:
        data = json.loads(raw)
    except ValueError:
        raise RuntimeError('NASA sent a page that is not JSON (captive portal, VPN or firewall?)')
    if isinstance(data, dict):                         # an error object instead of a list of entries
        raise RuntimeError(str(data.get('message') or data)[:150])
    new = found = 0
    for d in data:
        try:
            day = datetime.strptime(d['date'], '%Y-%m-%d')
        except (KeyError, TypeError, ValueError):
            continue
        title = html.unescape(d.get('title') or '').strip()
        explanation = d.get('explanation') or ''
        src = d.get('hdurl') or ''
        if not src:
            continue                                   # no photo (e.g. a video day): not listed, not sent to the webhook
        vals = {'ImageTitle': title, 'ImageSourceURL': src, 'ExplanationHTML': explanation,
                'Explanation': apod_text(explanation), 'GalleryURL': d.get('permalink') or '',
                'PublishedDate': day.strftime('%B %d, %Y')}
        new += add(APOD, d['date'], vals, title, vals['PublishedDate'], day.timestamp(), persist=False)
        found += 1
    if _dirty:
        save()
    return new, found


# ---------------------------------------------------------------------------------------------------------
# Charger events
# ---------------------------------------------------------------------------------------------------------

def read_battery():
    """Battery status in the same shape as termux-battery-status: {'plugged': ..., 'percentage': ...}."""
    if IS_ANDROID:
        Intent = jclass('android.content.Intent')
        IntentFilter = jclass('android.content.IntentFilter')
        status = android_context().registerReceiver(None, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        plugged = status.getIntExtra('plugged', 0)
        level, scale = status.getIntExtra('level', -1), status.getIntExtra('scale', 100)
        return {'plugged': 'PLUGGED' if plugged else 'UNPLUGGED',
                'percentage': int(level * 100 / scale) if level >= 0 and scale > 0 else ''}
    import subprocess
    return json.loads(subprocess.run(['termux-battery-status'], capture_output=True, text=True, timeout=10).stdout)


def charger_event(plugged, pct, late=False):
    """Log one charger event; in Automation mode the webhook is sent in the background."""
    aid, title = (PLUG, 'Charger plugged') if plugged else (UNPLUG, 'Charger unplugged')
    if not is_on(aid):
        return 0
    now = datetime.now()
    at = now.strftime('%B %d, %Y at %I:%M%p')
    pct = str(pct) if str(pct).lstrip('-').isdigit() and int(pct) >= 0 else ''
    sub = f'{pct}% \u00b7 {at}' if pct else at
    if late:
        sub += ' \u00b7 detected late'
    with lock:
        new = add(aid, str(time.time()), {'OccurredAt': at, 'BatteryLevel': pct}, title, sub, now.timestamp())
        entry_id = S['next'] - 1
    if new and is_auto(aid):
        threading.Thread(target=auto_post, args=(entry_id,), daemon=True).start()
    return new


def on_plug(plugged, pct=None):
    """Called once by PlugReceiver.java at the moment the charger is plugged in or unplugged."""
    global _last_plug_event
    now = time.time()
    with lock:
        if _last_plug_event[0] == plugged and now - _last_plug_event[1] < 3:    # duplicate broadcast
            return 0
        _last_plug_event = (plugged, now)
        S['plug'] = plugged                            # always recorded, even while the Applet is off
    new = charger_event(plugged, pct)                  # a new entry makes add() persist the state as well
    if not new:
        save()
    return new


def sync_plug_state():
    """Compare the current charger state with the last known one.

    Used at service start (to catch changes made while the service was down), by "Check now", and by the
    polling fallback on Termux/PC. Returns the number of new entries.
    """
    battery = read_battery()
    current = battery.get('plugged', 'UNPLUGGED') != 'UNPLUGGED'
    with lock:
        previous = S.get('plug')
        S['plug'] = current
        if previous != current:
            save()
    if previous is None or previous == current:
        return 0                                       # the first reading is a baseline, not an event
    if PLUG_EVENTS:
        # With real-time events the change happened while the service or receiver was down, so the time and
        # battery level would be wrong (they would be "now", not when it happened). Update the baseline
        # silently: no Activity entry and no webhook.
        return 0
    return charger_event(current, battery.get('percentage', ''))


def auto_post(entry_id):
    """Automation mode: send the webhook for a new entry, with up to 3 attempts.

    If it still fails, the error is shown in View activity and the manual "Post to Discord" button keeps working.
    """
    err = ''
    for wait in (0, 4, 15):
        if wait:
            time.sleep(wait)
        body, code = api_post(entry_id, 'auto')
        if code == 200:
            return
        err = str(body.get('error', code))
        if code in (400, 404):                         # configuration error: retrying will not help
            break
    with lock:
        entry = next((x for x in S['activity'] if x['id'] == entry_id), None)
        if entry:
            entry['err'] = err[:200]
            save()


def start_plug_receiver():
    """Android: register PlugReceiver so the system reports charger events and notification button taps."""
    global PLUG_EVENTS
    try:
        jclass('org.ifttw.PlugReceiver').start(android_context(), PORT)
        PLUG_EVENTS = True
    except Exception as e:
        print('plug receiver failed, falling back to polling:', e)


def worker():
    """Charger polling fallback for Termux/PC (also used on Android if the receiver failed to start).

    NASA is never polled: it is only checked through "Check now".
    """
    while True:
        if is_on(PLUG) or is_on(UNPLUG):
            try:
                sync_plug_state()
            except Exception as e:
                print('battery:', e)
        else:
            S.pop('plug', None)                        # both Applets are off: start from a fresh baseline later
        time.sleep(BAT_EVERY_ANDROID if IS_ANDROID else BAT_EVERY)


# ---------------------------------------------------------------------------------------------------------
# Android notifications
# ---------------------------------------------------------------------------------------------------------

def new_builder(ctx, nm, channel_id, channel_name):
    """Notification.Builder at the lowest importance (Minimized): no sound, pop-up or status-bar icon."""
    Builder = jclass('android.app.Notification$Builder')
    if sdk_int() >= 26:
        importance = jclass('android.app.NotificationManager').IMPORTANCE_MIN
        channel = jclass('android.app.NotificationChannel')(channel_id, channel_name, importance)
        channel.setShowBadge(False)
        nm.createNotificationChannel(channel)
        return Builder(ctx, channel_id)
    builder = Builder(ctx)                             # Android 7.x has no channels
    builder.setPriority(PRIORITY_MIN)
    return builder


def icon_bitmap():
    """The white-on-transparent notification icon as a Bitmap, or None if the file is missing."""
    path = os.path.join(RES_DIR, 'notif_icon.png')
    return jclass('android.graphics.BitmapFactory').decodeFile(path) if os.path.exists(path) else None


def set_small_icon(builder, ctx):
    bmp = icon_bitmap()
    if bmp is not None:
        builder.setSmallIcon(jclass('android.graphics.drawable.Icon').createWithBitmap(bmp))
    else:
        builder.setSmallIcon(ctx.getApplicationInfo().icon)


def broadcast_intent(ctx, action, code):
    """PendingIntent for a notification button: a broadcast to PlugReceiver, which this service registers."""
    PI = jclass('android.app.PendingIntent')
    intent = jclass('android.content.Intent')(action)
    intent.setPackage(ctx.getPackageName())
    return PI.getBroadcast(ctx, code, intent, PI.FLAG_UPDATE_CURRENT | PI.FLAG_IMMUTABLE)


def set_open_app_intent(builder, ctx):
    """Tapping the notification opens the app."""
    launch = ctx.getPackageManager().getLaunchIntentForPackage(ctx.getPackageName())
    if launch is not None:
        PI = jclass('android.app.PendingIntent')
        builder.setContentIntent(PI.getActivity(ctx, 0, launch, PI.FLAG_UPDATE_CURRENT | PI.FLAG_IMMUTABLE))


def add_action(builder, ctx, title, pending_intent):
    """Add a notification button: the modern Notification.Action first, the legacy overload as a fallback."""
    try:
        bmp = icon_bitmap()
        Icon = jclass('android.graphics.drawable.Icon')
        icon = (Icon.createWithBitmap(bmp) if bmp is not None
                else Icon.createWithResource(ctx, ctx.getApplicationInfo().icon))
        builder.addAction(jclass('android.app.Notification$Action$Builder')(icon, title, pending_intent).build())
    except Exception as e:
        nlog(f'{title} button (modern API) failed: {e}; trying the legacy API')
        builder.addAction(0, title, pending_intent)


def optional(what, fn):
    """Run an optional notification step. A failure is only logged, so the notification itself still appears."""
    try:
        fn()
        return True
    except Exception as e:
        nlog(f'{what} failed: {e}')
        return False


def notify_sent(aid, title=''):
    """Android: brief notification shown only after a webhook was sent successfully (automatic or manual).

    It is not ongoing: it can be swiped away and does not keep the service alive. Only one exists at a time,
    because a new one from any Applet replaces the old one. It uses the same Minimized level as "Running".
    Afterwards "Running" is posted again so that it stays above this notification (Android sorts by time).
    """
    if not IS_ANDROID:
        return
    try:
        ctx = android_context()
        nm = notification_manager(ctx)
        builder = new_builder(ctx, nm, SENT_CHANNEL, 'Webhook sent')
        set_small_icon(builder, ctx)
        default = APPLET_NAMES.get(aid, 'IFTTW')
        with lock:
            name = (S['applets'].get(aid, {}).get('title') or default).strip() or default
        builder.setContentTitle(name)
        builder.setContentText('Sent to webhook' + (f' \u00b7 {title}' if title else ''))
        builder.setAutoCancel(True)
        builder.setOnlyAlertOnce(True)
        set_open_app_intent(builder, ctx)
        nm.notify(SENT_ID, builder.build())
        time.sleep(0.05)                               # make sure "Running" gets a newer timestamp
        show_service_notification()
    except Exception as e:                             # a notification failure must never break sending
        print('notification:', e)


def cleanup_old_notification():
    """Android: remove the old "N Applets active" notification and the channels created by earlier versions."""
    try:
        nm = notification_manager(android_context())
        nm.cancel(NOTIF_ID)
        if sdk_int() >= 26:
            for channel in (NOTIF_CHANNEL,) + OLD_CHANNELS:
                try:                                   # one failed deletion must not stop the others
                    nm.deleteNotificationChannel(channel)
                except Exception as e:
                    print('delete channel', channel, ':', e)
    except Exception as e:
        print('notification cleanup:', e)


def load_notifier():
    """Android: load the Java class ServiceNotifier.

    Must run on the service's main thread (app classes cannot be loaded from Python-created threads). The
    result is kept in _notifier and shared by all threads.
    """
    global _notifier
    try:
        _notifier = jclass('org.ifttw.ServiceNotifier')
    except Exception as e:
        nlog(f'ServiceNotifier.java could not be loaded, using the Python fallback: {e}')


def show_service_notification():
    """Android: post the persistent "Running  ·  N Applets" notification with its Exit button.

    ServiceNotifier.java builds it, so the compiler checks every Android API call at build time and the button
    cannot silently disappear. The first call promotes the service to the foreground; later calls only update it.
    """
    global _foreground
    if not IS_ANDROID or _closing:
        return
    if _notifier is not None:
        try:
            err = _notifier.post(android_context(), os.path.join(RES_DIR, 'notif_icon.png'), running_title())
            _foreground = bool(_notifier.isForeground())
            if err:
                nlog(str(err))
            return
        except Exception as e:
            nlog(f'ServiceNotifier.post failed ({e}); using the Python fallback')
    _show_service_notification_py()


def _show_service_notification_py():
    """Fallback for show_service_notification(), used only when ServiceNotifier.java cannot be loaded.

    The essential parts (title, icon, startForeground) are kept apart from the optional ones (buttons,
    deleteIntent, ...), so a failure in one optional part no longer cancels the whole notification.
    """
    global _foreground
    if not IS_ANDROID or _closing:
        return
    try:
        ctx = android_context()
        nm = notification_manager(ctx)
        builder = new_builder(ctx, nm, SVC_CHANNEL, 'IFTTW service')
        set_small_icon(builder, ctx)
        builder.setContentTitle(running_title())
        builder.setOngoing(True)
        builder.setOnlyAlertOnce(True)
        builder.setShowWhen(False)
        if sdk_int() >= 31:                            # Android 12+: without this the notification appears ~10 s late
            optional('foreground behavior', lambda: builder.setForegroundServiceBehavior(FOREGROUND_SERVICE_IMMEDIATE))
        optional('deleteIntent', lambda: builder.setDeleteIntent(broadcast_intent(ctx, ACTION_REPOST, 3)))
        optional('Exit button', lambda: add_action(builder, ctx, 'Exit', broadcast_intent(ctx, ACTION_EXIT, 1)))
        optional('content intent', lambda: set_open_app_intent(builder, ctx))
        note = builder.build()
        optional('notification flags', lambda: setattr(note, 'flags', note.flags | FLAG_NO_CLEAR))
        if not _foreground:
            try:
                ctx.startForeground(SVC_ID, note)
                _foreground = True
                nlog('startForeground succeeded')
            except Exception as e:                     # e.g. Android forbids starting a foreground service from the background
                nlog(f'startForeground failed: {e}')
                nm.notify(SVC_ID, note)
        else:
            nm.notify(SVC_ID, note)
    except Exception as e:
        nlog(f'service notification failed completely: {e}')


# ---------------------------------------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------------------------------------

def close_screen():
    """Close the app screen (Activity) and remove it from the recent apps list."""
    try:
        tasks = activity_manager(android_context()).getAppTasks()
        for i in range(tasks.size()):
            try:
                tasks.get(i).finishAndRemoveTask()
            except Exception as e:
                print('close screen:', e)
    except Exception as e:
        print('close screen:', e)


def screen_process():
    """(importance, pid) of the screen process, or None if it is gone. The WebView runs in the main process."""
    ctx = android_context()
    am = activity_manager(ctx)
    pkg = ctx.getPackageName()
    procs = am.getRunningAppProcesses()
    if procs is not None:
        for i in range(procs.size()):
            p = procs.get(i)
            if p.processName == pkg:
                return p.importance, p.pid
    return None


def kill_screen_process():
    """Kill the screen process too, so the result matches Force Close: no IFTTW process is left."""
    try:
        info = screen_process()
        if info:
            jclass('android.os.Process').killProcess(info[1])
    except Exception as e:
        print('kill screen process:', e)


def final_kill():
    kill_screen_process()
    os._exit(0)                                        # then this service process


def do_exit():
    """Exit button, or the last Applet was turned off: close the screen, stop the service, clear the notification
    and kill every process."""
    global _foreground, _closing
    if _closing:
        return
    _closing = True
    close_screen()
    try:
        ctx = android_context()
        ctx.stopForeground(True)
        notification_manager(ctx).cancel(SVC_ID)
        _foreground = False
        ctx.stopSelf()
    except Exception as e:
        print('stop service:', e)
    threading.Timer(0.8, final_kill).start()           # let the screen close and the HTTP reply go out first


def _exit_if_idle():
    if not any_active():
        do_exit()


def cancel_exit():
    global _exit_timer
    timer, _exit_timer = _exit_timer, None
    if timer is not None:
        timer.cancel()


def schedule_exit():
    """The last Applet was just turned off: close the app shortly, unless an Applet is turned on again."""
    global _exit_timer
    cancel_exit()
    _exit_timer = threading.Timer(EXIT_DELAY, _exit_if_idle)
    _exit_timer.daemon = True
    _exit_timer.start()


def idle_watch():
    """The app was opened while no Applet is connected.

    The screen needs the server, so the service stays alive while the screen is in use. Once the screen has been
    left for two consecutive checks and no Applet is active, close completely. Stops by itself as soon as an
    Applet is turned on.
    """
    away = 0
    while not _closing:
        time.sleep(IDLE_CHECK)
        if any_active():
            return
        try:
            info = screen_process()
            seen = info is not None and info[0] <= IMPORTANCE_VISIBLE
        except Exception as e:
            print('idle watch:', e)
            seen = True                                # not sure: do not close
        away = 0 if seen else away + 1
        if away >= 2:
            do_exit()
            return


def service_ctl(cmd):
    """Handle a notification button, forwarded by PlugReceiver.java. Only meaningful on Android."""
    if not IS_ANDROID:
        return False
    if cmd == 'exit':
        do_exit()
        return True
    if cmd == 'repost':
        show_service_notification()
        return True
    return False


def diag():
    """Diagnostics for GET /api/diag, to find out why the persistent notification is missing.

    Contains no webhook URLs or other personal data.
    """
    d = {'android': IS_ANDROID, 'foreground': _foreground, 'closing': _closing, 'plug_events': PLUG_EVENTS,
         'java_notifier': _notifier is not None, 'any_active': any_active(), 'log': list(NOTIF_LOG)}
    if IS_ANDROID:
        try:
            nm = notification_manager(android_context())
            d['sdk'] = sdk_int()
            d['notifications_enabled'] = bool(nm.areNotificationsEnabled())
            if d['sdk'] >= 26:
                channel = nm.getNotificationChannel(SVC_CHANNEL)
                d['channel_importance'] = channel.getImportance() if channel is not None else None   # 0 = blocked, 1 = minimized (expected)
            posted = list(nm.getActiveNotifications())
            d['posted_ids'] = [n.getId() for n in posted]          # should contain 7002 (the Running notification)
            for n in posted:
                if n.getId() == SVC_ID:                            # buttons actually attached: expected ['Exit']
                    actions = n.getNotification().actions
                    d['running_actions'] = [str(a.title) for a in actions] if actions else []
        except Exception as e:
            d['diag_error'] = str(e)
    return d


# ---------------------------------------------------------------------------------------------------------
# Applet API
# ---------------------------------------------------------------------------------------------------------

def check_applet(aid):
    """One manual check ("Check now"). The result is always recorded, on success and on failure, so the screen
    can display it."""
    result = {'at': time.time(), 'ok': False, 'found': 0, 'new': 0, 'err': ''}
    try:
        if not is_on(aid):
            result['err'] = 'This Applet is not connected. Turn it on first.'
        elif aid in (IOTD, APOD):
            result['new'], result['found'] = (poll_iotd if aid == IOTD else poll_apod)()
            result['ok'] = True
        else:
            result['new'] = sync_plug_state()
            result['ok'] = True
    except Exception as e:
        result['err'] = error_reason(e)
        print('check', aid, ':', e)
    CHECKS[aid] = result
    return result


def render(template, vals, js):
    """Replace {{name}} placeholders with values. With js=True the values are escaped for use inside a JSON string."""
    def substitute(m):
        value = str(vals.get(m.group(1), ''))
        return json.dumps(value)[1:-1] if js else value
    return re.sub(r'\{\{(\w+)\}\}', substitute, template)


def state_view():
    """Applet settings plus 'since' (connected date) and 'last' (latest activity) for the detail page."""
    with lock:
        meta = S.get('meta', {})
        return json.loads(json.dumps({aid: {**applet, **meta.get(aid, {})} for aid, applet in S['applets'].items()}))


def api_set_applet(aid, d):
    with lock:
        had_active = any_active()
        count_before = active_count()
        applet = S['applets'].setdefault(aid, {'run': 0})
        meta = meta_of(aid)
        was_on = bool(applet.get('on', 0)) and 'since' in meta
        applet.update({k: d[k] for k in KEYS if k in d})
        if applet.get('on', 1) and not was_on:
            meta['since'] = time.time()                # newly connected
        save()
        active = any_active()
        count_after = active_count()
    sync_idle_flag()
    if count_after != count_before:
        show_service_notification()                    # the number in "Running  ·  N Applets" changed
    closing = False
    if IS_ANDROID:
        if active:
            cancel_exit()                              # an Applet was turned back on before the app closed
        elif had_active:
            # Only on the "some active" -> "none active" transition, so saving Configure while every Applet is
            # already off does not close the app.
            schedule_exit()
            closing = True
    return {'ok': 1, 'closing': closing}, 200


def api_activity(aid):
    with lock:
        rows = sorted((x for x in S['activity'] if x['aid'] == aid), key=lambda x: -x['ts'])[:SHOW_ACTIVITY]
        return [{k: x.get(k) for k in ('id', 'title', 'sub', 'img', 'posted', 'by', 'err')} for x in rows], 200


def api_check(d=None):
    aid = (d or {}).get('aid')
    ids = [aid] if aid in APPLET_NAMES else list(APPLET_NAMES)     # no aid: check every Applet
    results = {a: check_applet(a) for a in ids}
    return {'new': sum(r['new'] for r in results.values()),
            'errors': [f'{a}: {r["err"]}' for a, r in results.items() if r['err']],
            'results': results}, 200


def api_post(entry_id, by='manual'):
    """Send the webhook for one Activity entry; return (response body, HTTP status)."""
    with lock:
        entry = next((x for x in S['activity'] if x['id'] == entry_id), None)
    if not entry:
        return {'error': 'Entry not found'}, 404
    applet = S['applets'].get(entry['aid'])
    if not applet:
        return {'error': 'Reload the page first'}, 400
    url = render(applet['u'], entry['vals'], False)
    if 'ID/TOKEN' in url or not url.startswith('http'):
        return {'error': 'Set the Discord webhook URL in Configure first'}, 400
    headers = {'Content-Type': applet['ct']}
    for line in applet['h'].splitlines():
        if ':' in line:
            name, value = line.split(':', 1)
            headers[name.strip()] = render(value.strip(), entry['vals'], False)
    body = render(applet['b'], entry['vals'], 'json' in applet['ct']).encode('utf-8')
    try:
        http_request(url, body, headers, applet['m'])
    except urllib.error.HTTPError as e:
        return {'error': f'Webhook rejected the request ({e.code}): ' + e.read()[:200].decode(errors='ignore')}, 502
    except Exception as e:
        return {'error': str(e)}, 502
    with lock:
        entry['posted'], entry['by'] = time.time(), by
        entry.pop('err', None)
        applet['run'] = applet.get('run', 0) + 1
        meta_of(entry['aid'])['last'] = time.time()
        save()
        runs, aid, title = applet['run'], entry['aid'], entry.get('title', '')
    notify_sent(aid, title)                            # only after the webhook was actually delivered
    return {'ok': 1, 'run': runs}, 200


# ---------------------------------------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------------------------------------

def read_file(path):
    with open(path, 'rb') as f:
        return f.read()


def parse_json_object(raw):
    """Decode a request body as a JSON object; return None if it is not valid JSON or not an object."""
    try:
        data = json.loads(raw.decode('utf-8') or '{}')
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'IFTTW'

    def log_message(self, *args):
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8', cache='no-store'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def reply(self, result):
        body, code = result
        self.send(code, body)

    def json_body(self, raw):
        """Parse the request body as a JSON object. On failure a 400 is sent and None is returned."""
        data = parse_json_object(raw)
        if data is None:
            self.send(400, {'error': 'Invalid JSON'})
        return data

    def route(self, method):
        path = urllib.parse.urlsplit(self.path).path
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length > 0 else b''      # always drain the body to keep the connection clean
        if method == 'GET':
            self.route_get(path)
        else:
            self.route_post(path, raw)

    def route_get(self, path):
        if path == '/':
            return self.send(200, read_file(os.path.join(WEB_DIR, 'index.html')), 'text/html; charset=utf-8')
        font = re.fullmatch(r'/fonts/([\w.-]+\.woff2?)', path)    # bundled UI fonts; the name has no '/', so it cannot leave the folder
        if font:
            fp = os.path.join(WEB_DIR, 'fonts', font.group(1))
            if os.path.isfile(fp):
                return self.send(200, read_file(fp), 'font/' + font.group(1).rsplit('.', 1)[1],
                                 'public, max-age=604800')
        if path == '/api/state':
            return self.send(200, state_view())
        if path == '/api/diag':                                   # on Termux: curl http://127.0.0.1:5000/api/diag
            return self.send(200, diag())
        activity = re.fullmatch(r'/api/activity/([^/]+)', path)
        if activity:
            return self.reply(api_activity(urllib.parse.unquote(activity.group(1))))
        self.send(404, {'error': 'Not found'})

    def route_post(self, path, raw):
        if path == '/api/check':
            return self.reply(api_check(parse_json_object(raw) or {}))
        if path == '/api/plug':                                   # PlugReceiver.java: charger plugged / unplugged
            d = self.json_body(raw)
            if d is not None:
                self.send(200, {'new': on_plug(bool(d.get('plugged')), d.get('percentage'))})
            return
        if path == '/api/ctl':                                    # PlugReceiver.java: notification button pressed
            d = self.json_body(raw)
            if d is not None:
                self.send(200, {'ok': service_ctl(d.get('cmd'))})
            return
        applet = re.fullmatch(r'/api/applet/([^/]+)', path)
        if applet:
            d = self.json_body(raw)
            if d is not None:
                self.reply(api_set_applet(urllib.parse.unquote(applet.group(1)), d))
            return
        post = re.fullmatch(r'/api/post/(\d+)', path)
        if post:
            return self.reply(api_post(int(post.group(1))))
        self.send(404, {'error': 'Not found'})

    def handle_any(self, method):
        try:
            self.route(method)
        except Exception as e:
            print('http:', e)
            try:
                self.send(500, {'error': str(e)})
            except Exception:
                pass

    def do_GET(self):
        self.handle_any('GET')

    def do_HEAD(self):
        self.handle_any('GET')

    def do_POST(self):
        self.handle_any('POST')


def run():
    if IS_ANDROID:
        try:
            for name in JAVA_PRELOAD:
                jclass(name)
            android_context()
        except Exception as e:
            print('android init:', e)
        load_notifier()
        show_service_notification()                    # first: the service becomes a foreground service right away
        start_plug_receiver()                          # also on the main thread, for the same class-loading reason
        if PLUG_EVENTS:
            try:
                sync_plug_state()                      # catch charger changes made while the service was down
            except Exception as e:
                print('battery:', e)
        cleanup_old_notification()
        sync_idle_flag()
        if not any_active():                           # opened with no active Applet: close once the screen is left
            threading.Thread(target=idle_watch, daemon=True).start()
    if not PLUG_EVENTS:                                # polling is only the fallback (Termux/PC or a failed receiver)
        threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    server.daemon_threads = True
    # The default poll_interval of 0.5 s wakes the process twice a second forever. The server is never shut
    # down (the process is simply killed), so a long interval is safe and lets the CPU stay idle.
    server.serve_forever(poll_interval=3600)


if __name__ == '__main__':
    run()
