package org.ifttw;

import android.app.ActivityManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.os.Process;
import android.util.Log;

import java.util.List;

/**
 * Persistent "Running" notification that shows the number of active Applets (for example "4 Applets"). It
 * follows the Termux style: Minimized importance level and an "Exit" button.
 *
 * It is written in Java rather than through pyjnius because the compiler checks every Android API call at build
 * time, so the button can no longer disappear silently because of a wrong overload or argument type in Python.
 * app.py only calls post(): the title is built in Python (running_title) and passed in. post() returns ""
 * on success, otherwise an error text (recorded in /api/diag).
 *
 * The buttons broadcast to PlugReceiver (ACTION_EXIT / ACTION_REPOST).
 */
public class ServiceNotifier {
    public static final int ID = 7002;
    // A channel's importance (Minimized here) cannot change after the channel is created, hence a fresh id.
    // Must match SVC_CHANNEL in app.py.
    public static final String CHANNEL = "ifttw_running_min";
    private static final String TAG = "IFTTW";

    private static Service service;
    private static boolean foreground;

    /** Shows or updates the notification. The first call promotes the service to the foreground. */
    public static synchronized String post(Service svc, String iconPath, String title) {
        String warn = "";
        try {
            service = svc;
            NotificationManager nm = (NotificationManager) svc.getSystemService(Context.NOTIFICATION_SERVICE);

            Notification.Builder b;
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(CHANNEL, "IFTTW service", NotificationManager.IMPORTANCE_MIN);
                ch.setShowBadge(false);
                nm.createNotificationChannel(ch);
                b = new Notification.Builder(svc, CHANNEL);
            } else {
                b = new Notification.Builder(svc);
                b.setPriority(Notification.PRIORITY_MIN);       // Android 7.x has no channels
            }

            Icon icon = smallIcon(svc, iconPath);
            b.setSmallIcon(icon);
            b.setContentTitle(title);                       // title only: no second line of text
            b.setOngoing(true);
            b.setOnlyAlertOnce(true);
            b.setShowWhen(false);
            if (Build.VERSION.SDK_INT >= 31) {
                b.setForegroundServiceBehavior(Notification.FOREGROUND_SERVICE_IMMEDIATE);
            }

            // The button is a core part of this notification, not an optional extra.
            b.addAction(new Notification.Action.Builder(icon, "Exit",
                    broadcast(svc, PlugReceiver.ACTION_EXIT, 1)).build());

            // Tap: open the app. Swipe away (Android 14+): post it again.
            Intent launch = svc.getPackageManager().getLaunchIntentForPackage(svc.getPackageName());
            if (launch != null) {
                b.setContentIntent(PendingIntent.getActivity(svc, 0, launch,
                        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE));
            }
            b.setDeleteIntent(broadcast(svc, PlugReceiver.ACTION_REPOST, 3));

            Notification n = b.build();
            n.flags |= Notification.FLAG_NO_CLEAR;      // survives "Clear all"

            if (!foreground) {
                try {
                    svc.startForeground(ID, n);
                    foreground = true;
                } catch (Throwable t) {                 // e.g. Android forbids starting a foreground service from the background
                    Log.e(TAG, "startForeground failed", t);
                    warn = "startForeground failed: " + t;
                    nm.notify(ID, n);
                }
            } else {
                nm.notify(ID, n);
            }
            return warn;
        } catch (Throwable t) {
            Log.e(TAG, "service notification failed", t);
            return "notification failed: " + t;
        }
    }

    public static synchronized boolean isForeground() {
        return foreground;
    }

    private static PendingIntent broadcast(Context ctx, String action, int code) {
        Intent i = new Intent(action);
        i.setPackage(ctx.getPackageName());
        return PendingIntent.getBroadcast(ctx, code, i, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private static Icon smallIcon(Context ctx, String path) {
        try {
            if (path != null && path.length() > 0) {
                Bitmap bmp = BitmapFactory.decodeFile(path);   // white logo on transparent (ifttw/res/notif_icon.png)
                if (bmp != null) {
                    return Icon.createWithBitmap(bmp);
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "notification icon", t);
        }
        return Icon.createWithResource(ctx, ctx.getApplicationInfo().icon);
    }

    /**
     * Backup for the Exit button: if Python has not closed the process after a few seconds (stuck, or the
     * server does not answer), close everything from here. If Python succeeds, the process is already gone
     * and this thread disappears with it.
     */
    public static void exitWatchdog(final Context ctx, final long delayMs) {
        Thread t = new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    Thread.sleep(delayMs);
                } catch (InterruptedException e) {
                    return;
                }
                hardExit(ctx);
            }
        });
        t.setDaemon(true);
        t.start();
    }

    /** Closes the screen, service and notification, then every IFTTW process (the same result as Force Close). */
    public static void hardExit(final Context ctx) {
        try {
            ActivityManager am = (ActivityManager) ctx.getSystemService(Context.ACTIVITY_SERVICE);
            List<ActivityManager.AppTask> tasks = am.getAppTasks();       // the app screen; also removes it from recents
            if (tasks != null) {
                for (ActivityManager.AppTask t : tasks) {
                    try {
                        t.finishAndRemoveTask();
                    } catch (Throwable ignored) {
                        // continue with the next task
                    }
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "close screen", t);
        }
        try {
            NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            nm.cancel(ID);
            Service s;
            synchronized (ServiceNotifier.class) {
                s = service;
                foreground = false;
            }
            if (s != null) {
                s.stopForeground(true);
                s.stopSelf();
            }
        } catch (Throwable t) {
            Log.w(TAG, "stop service", t);
        }
        try {
            Thread.sleep(600);                                            // give the screen time to close
        } catch (InterruptedException ignored) {
            // carry on
        }
        try {
            ActivityManager am = (ActivityManager) ctx.getSystemService(Context.ACTIVITY_SERVICE);
            List<ActivityManager.RunningAppProcessInfo> procs = am.getRunningAppProcesses();
            if (procs != null) {
                for (ActivityManager.RunningAppProcessInfo p : procs) {
                    if (ctx.getPackageName().equals(p.processName) && p.pid != Process.myPid()) {
                        Process.killProcess(p.pid);                       // the screen process (WebView)
                    }
                }
            }
        } catch (Throwable t) {
            Log.w(TAG, "kill screen process", t);
        }
        Process.killProcess(Process.myPid());                             // last: this service process
    }
}
