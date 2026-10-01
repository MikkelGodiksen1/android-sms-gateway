# SENEL fork

This is a fork of [capcom6/android-sms-gateway](https://github.com/capcom6/android-sms-gateway) (Apache-2.0).
It runs on one salesman's Android phone in Cloud Server mode against `api.sms-gate.app`.
Upstream builds delay outgoing SMS, status reports and incoming-SMS webhooks by minutes to hours while the phone is idle (Doze),
because the work they trigger is scheduled as regular WorkManager jobs. This fork makes that work run right away.

## Changes (branch `reliability`)

1. **Upstream PR #449** (cherry-picked, original author kept): push, SSE and ping trigger an immediate expedited pull
   (`PullMessagesWorker.startOnce`) instead of rescheduling the 15-minute periodic job.
   Files: `gateway/EventsReceiver.kt`, `gateway/services/SSEForegroundService.kt`, `gateway/workers/PullMessagesWorker.kt`.
2. **Expedited status reporting**: `gateway/workers/SendStateWorker.kt` is enqueued as expedited work and has a foreground notification.
3. **Expedited webhook forwarding**: `webhooks/workers/WebhookQueueProcessorWorker.kt` is expedited when it runs without an initial delay.
4. **SSE by default**: `gateway/GatewaySettings.kt` and `res/xml/cloud_server_preferences.xml` default the notification channel to `SSE_ONLY`.

Notification ids and strings for the new foreground notifications live in `notifications/NotificationsService.kt` and `res/values/strings.xml`.

## No Firebase

This fork has no `google-services.json` from upstream, so it cannot receive FCM pushes.
The build writes a placeholder config, and the app uses the SSE connection (`SSEForegroundService`) instead.
That is why `SSE_ONLY` is the default.

## Build

Run the **senel-build** workflow (Actions tab, "Run workflow") on the `reliability` branch with a version name such as `1.77.1-senel.1`.
It needs the secrets `SIGNING_KEY_STORE_BASE64`, `SIGNING_KEY_ALIAS`, `SIGNING_KEY_PASSWORD` and `SIGNING_STORE_PASSWORD`.
It publishes `sms-gateway-senel.apk` as a GitHub Release tagged `senel-v<version>`.

## Install

The APK is signed with our own key, not upstream's. Uninstall the official app first, then install this one.
Later builds from this workflow install as updates over each other.
