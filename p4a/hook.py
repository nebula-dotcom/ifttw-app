"""python-for-android hook: add BootReceiver to AndroidManifest.xml so the IFTTW bot service starts by itself
after the phone boots. Buildozer runs it through `p4a.hook` in buildozer.spec.

Everything is wrapped in try/except: if the hook fails, the APK is still built (just without auto-start) and
the reason is printed in the build log.
"""
from pathlib import Path

RECEIVER = """
        <receiver android:name="org.ifttw.BootReceiver" android:enabled="true" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.BOOT_COMPLETED" />
                <action android:name="android.intent.action.QUICKBOOT_POWERON" />
                <action android:name="com.htc.intent.action.QUICKBOOT_POWERON" />
                <action android:name="android.intent.action.MY_PACKAGE_REPLACED" />
            </intent-filter>
        </receiver>
"""


def after_apk_build(toolchain):
    try:
        manifest = Path(toolchain._dist.dist_dir) / 'src' / 'main' / 'AndroidManifest.xml'
        text = manifest.read_text(encoding='utf-8')
        if 'org.ifttw.BootReceiver' in text:
            print('IFTTW hook: BootReceiver is already in the manifest')
        elif '</application>' not in text:
            print('IFTTW hook: </application> tag not found, BootReceiver was NOT added')
        else:
            manifest.write_text(text.replace('</application>', RECEIVER + '    </application>', 1), encoding='utf-8')
            print('IFTTW hook: BootReceiver added to AndroidManifest.xml')
    except Exception as e:
        print('IFTTW hook: failed to add BootReceiver ->', e)
