package org.ifttw;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Build;

import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;

/**
 * Receives Android broadcasts in real time (no polling) and forwards each one exactly once to the local
 * IFTTW server:
 *   - charger plugged / unplugged                       -> POST http://127.0.0.1:PORT/api/plug
 *   - "Exit" button on the persistent notification      -> POST http://127.0.0.1:PORT/api/ctl {"cmd":"exit"}
 *   - notification swiped away (deleteIntent)           -> POST /api/ctl {"cmd":"repost"}, so it appears again
 *
 * Registered from Python (app.py: start_plug_receiver) inside the service, so it lives as long as the service.
 */
public class PlugReceiver extends BroadcastReceiver {
    // Must match ACTION_EXIT / ACTION_REPOST in app.py.
    public static final String ACTION_EXIT = "org.ifttw.action.EXIT";
    public static final String ACTION_REPOST = "org.ifttw.action.REPOST";

    private static PlugReceiver instance;
    private static int port = 5000;

    public static synchronized void start(Context ctx, int serverPort) {
        if (instance != null) {
            return;                                  // already registered
        }
        port = serverPort;
        PlugReceiver r = new PlugReceiver();
        IntentFilter f = new IntentFilter();
        f.addAction(Intent.ACTION_POWER_CONNECTED);
        f.addAction(Intent.ACTION_POWER_DISCONNECTED);
        f.addAction(ACTION_EXIT);
        f.addAction(ACTION_REPOST);
        Context app = ctx.getApplicationContext();
        if (Build.VERSION.SDK_INT >= 33) {
            app.registerReceiver(r, f, Context.RECEIVER_NOT_EXPORTED);
        } else {
            app.registerReceiver(r, f);
        }
        instance = r;
    }

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || intent.getAction() == null) {
            return;
        }
        String action = intent.getAction();

        if (ACTION_EXIT.equals(action)) {
            forward("/api/ctl", "{\"cmd\":\"exit\"}");
            // Safety net: if Python has not closed the process within 6 seconds, close everything from Java.
            ServiceNotifier.exitWatchdog(context.getApplicationContext(), 6000);
            return;
        }
        if (ACTION_REPOST.equals(action)) {
            forward("/api/ctl", "{\"cmd\":\"repost\"}");
            return;
        }

        final boolean plugged;
        if (Intent.ACTION_POWER_CONNECTED.equals(action)) {
            plugged = true;
        } else if (Intent.ACTION_POWER_DISCONNECTED.equals(action)) {
            plugged = false;
        } else {
            return;
        }

        // Battery level at the time of the event, read from the sticky broadcast (no new receiver needed).
        int pct = -1;
        Intent b = context.registerReceiver(null, new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
        if (b != null) {
            int level = b.getIntExtra("level", -1);
            int scale = b.getIntExtra("scale", 100);
            if (level >= 0 && scale > 0) {
                pct = level * 100 / scale;
            }
        }
        forward("/api/plug", "{\"plugged\":" + plugged + ",\"percentage\":" + pct + "}");
    }

    /** Sends the request to the local server on a separate thread; goAsync() keeps the process alive until it is done. */
    private void forward(final String path, final String json) {
        final PendingResult pending = goAsync();
        new Thread(new Runnable() {
            @Override
            public void run() {
                try {
                    for (int i = 0; i < 3; i++) {            // the local server may have only just started
                        try {
                            send(path, json);
                            break;
                        } catch (Exception e) {
                            Thread.sleep(1500);
                        }
                    }
                } catch (InterruptedException ignored) {
                    // Just finish; Python catches up through sync_plug_state when the service next starts.
                } finally {
                    pending.finish();
                }
            }
        }).start();
    }

    private static void send(String path, String json) throws Exception {
        byte[] body = json.getBytes("UTF-8");
        String head = "POST " + path + " HTTP/1.1\r\n"
                + "Host: 127.0.0.1\r\n"
                + "Content-Type: application/json\r\n"
                + "Content-Length: " + body.length + "\r\n"
                + "Connection: close\r\n\r\n";
        Socket s = new Socket();
        try {
            s.connect(new InetSocketAddress("127.0.0.1", port), 3000);
            s.setSoTimeout(8000);
            OutputStream out = s.getOutputStream();
            out.write(head.getBytes("UTF-8"));
            out.write(body);
            out.flush();
            InputStream in = s.getInputStream();
            byte[] buf = new byte[256];
            while (in.read(buf) > 0) {
                // Drain the response so the server closes the connection cleanly.
            }
        } finally {
            s.close();
        }
    }
}
