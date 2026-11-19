package dev.damai;

import android.os.Build;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
import android.view.MotionEvent;

import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStreamReader;
import java.io.PrintWriter;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;

/** 用已获授权的 adb shell 身份，只向启动时指定的应用 UID 分发触摸。 */
public final class ScopedInput {
    // Android InputEventInjectionSync.WAIT_FOR_RESULT 等待分发器判定目标是否接受事件。
    private static final int WAIT_FOR_RESULT = 1;
    private final int uid;
    private final Object manager;
    private final Method injectToUid;
    private final MotionEvent.PointerProperties[] finger = {new MotionEvent.PointerProperties()};
    private final MotionEvent.PointerCoords[] point = {new MotionEvent.PointerCoords()};

    /** 只解析带 targetUid 的三参数接口。系统限制访问或服务不可用时启动失败。 */
    private ScopedInput(int uid) throws ReflectiveOperationException {
        this.uid = uid;
        Class<?> type = Class.forName("android.hardware.input.InputManagerGlobal");
        injectToUid = type.getMethod("injectInputEvent", InputEvent.class, int.class, int.class);
        manager = type.getMethod("getInstance").invoke(null);
        if (manager == null) throw new IllegalStateException("Android input service unavailable");
        finger[0].id = 0;
        finger[0].toolType = MotionEvent.TOOL_TYPE_FINGER;
        point[0].size = 1;
    }

    /** DOWN 和 UP 都独立交给 Android 校验。两个事件之间切换到别的应用也会被拒绝。 */
    private boolean dispatch(int action, long downTime) throws ReflectiveOperationException {
        point[0].pressure = action == MotionEvent.ACTION_DOWN ? 1 : 0;
        MotionEvent event = MotionEvent.obtain(downTime, SystemClock.uptimeMillis(), action, 1,
                finger, point, 0, 0, 1, 1, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            return Boolean.TRUE.equals(injectToUid.invoke(manager, event, WAIT_FOR_RESULT, uid));
        } catch (InvocationTargetException e) {
            // AOSP InputManagerService 遇到目标窗口 UID 不匹配时抛 IllegalArgumentException。
            if (e.getCause() instanceof IllegalArgumentException) return false;
            throw e;
        } finally {
            event.recycle();
        }
    }

    /** 同一触摸共用 downTime。DOWN 被拒绝时不发送 UP，任何失败都交回上层停止。 */
    private boolean tap(int x, int y) throws ReflectiveOperationException {
        if (x < 0 || y < 0) throw new IllegalArgumentException("Expected nonnegative coordinates");
        point[0].x = x;
        point[0].y = y;
        long downTime = SystemClock.uptimeMillis();
        return dispatch(MotionEvent.ACTION_DOWN, downTime) && dispatch(MotionEvent.ACTION_UP, downTime);
    }

    /** 每条 TAP 在两次系统判定后回复一次，拒绝或协议错误直接结束整个会话。 */
    public static void main(String[] args) {
        PrintWriter reply = new PrintWriter(new FileOutputStream(FileDescriptor.out), true);
        try {
            // 小米 15 当前为 Android 17。旧版服务（如 Android 13）在目标不匹配时可能改为全局注入。
            if (Build.VERSION.SDK_INT != 37)
                throw new IllegalStateException("UID-scoped input requires the validated Android 17 dispatcher");
            if (args.length != 1) throw new IllegalArgumentException("Expected application UID");
            int uid = Integer.parseInt(args[0]);
            int appId = uid % 100000;  // Android 为每个用户保留十万个 UID。
            if (appId < 10000 || appId > 19999) throw new IllegalArgumentException("Invalid app UID");
            ScopedInput input = new ScopedInput(uid);
            BufferedReader reader = new BufferedReader(new InputStreamReader(
                    new FileInputStream(FileDescriptor.in), StandardCharsets.US_ASCII));
            reply.println("READY " + uid);
            String line;
            while ((line = reader.readLine()) != null && !line.equals("QUIT")) {
                String[] parts = line.split(" ", -1);
                if (parts.length != 3 || !parts[0].equals("TAP"))
                    throw new IllegalArgumentException("Expected TAP x y");
                if (!input.tap(Integer.parseInt(parts[1]), Integer.parseInt(parts[2]))) {
                    reply.println("REJECT");
                    return;
                }
                reply.println("OK");
            }
        } catch (Exception | LinkageError e) {
            reply.println("ERROR");
            Throwable reason = e instanceof InvocationTargetException ? e.getCause() : e;
            new PrintWriter(new FileOutputStream(FileDescriptor.err), true).println(reason);
        }
    }
}
