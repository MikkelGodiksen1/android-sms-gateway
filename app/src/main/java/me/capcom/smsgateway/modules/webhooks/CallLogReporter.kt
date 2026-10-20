package me.capcom.smsgateway.modules.webhooks

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.database.ContentObserver
import android.net.Uri
import android.os.Handler
import android.os.HandlerThread
import android.provider.CallLog
import androidx.core.content.ContextCompat
import com.google.gson.GsonBuilder
import com.google.gson.JsonObject
import kotlinx.coroutines.runBlocking
import me.capcom.smsgateway.domain.EntitySource
import me.capcom.smsgateway.extensions.configure
import me.capcom.smsgateway.modules.gateway.GatewaySettings
import me.capcom.smsgateway.modules.localserver.LocalServerSettings
import me.capcom.smsgateway.modules.logs.LogsService
import me.capcom.smsgateway.modules.logs.db.LogEntry
import me.capcom.smsgateway.modules.receiver.StateStorage
import me.capcom.smsgateway.modules.webhooks.db.WebHook
import me.capcom.smsgateway.modules.webhooks.db.WebHooksDao
import me.capcom.smsgateway.modules.webhooks.db.WebhookQueueRepository
import me.capcom.smsgateway.modules.webhooks.workers.WebhookQueueProcessorWorker
import org.koin.core.component.KoinComponent
import org.koin.core.component.inject
import java.util.Date
import java.util.UUID

/**
 * Reports new call log rows (incoming, missed, outgoing, rejected) to the CRM.
 *
 * The observer lives as long as the foreground service that calls [start] / [stop].
 * Rows are posted directly to the locally stored sms status webhook (`.../api/sms/status?k=...`)
 * through the persistent webhook queue, so they use the same HTTP client, headers and signing
 * as every other webhook and never go through the cloud relay.
 *
 * The feature is silently off while READ_CALL_LOG is not granted or no such webhook exists.
 * On the first enable the high-water mark is set to the current max `_ID`, so history is
 * never backfilled.
 */
class CallLogReporter : KoinComponent {
    private val context: Context by inject()
    private val storage: StateStorage by inject()
    private val webHooksDao: WebHooksDao by inject()
    private val queueRepository: WebhookQueueRepository by inject()
    private val webhooksSettings: WebhooksSettings by inject()
    private val gatewaySettings: GatewaySettings by inject()
    private val localServerSettings: LocalServerSettings by inject()
    private val logsService: LogsService by inject()

    private val lock = Any()
    private var active = false
    private var handlerThread: HandlerThread? = null
    private var handler: Handler? = null
    private var observer: ContentObserver? = null

    @Volatile
    private var permissionMissingLogged = false

    @Volatile
    private var destinationMissingLogged = false

    /**
     * Called by the foreground service when it is created.
     */
    fun start() {
        synchronized(lock) {
            active = true
            register()
        }
    }

    /**
     * Called by the foreground service when it is destroyed.
     */
    fun stop() {
        synchronized(lock) {
            active = false
            unregister()
        }
    }

    /**
     * Re-checks the permission, e.g. when the app is resumed. Registers the observer if the
     * service is running and the permission was granted in the meantime, and catches up rows.
     */
    fun recheck() {
        synchronized(lock) {
            if (!active) return

            if (observer == null) {
                register()
            } else {
                handler?.post { processNewCalls() }
            }
        }
    }

    private fun register() {
        if (observer != null) return

        if (!canReadCallLog()) {
            logPermissionMissingOnce()
            return
        }
        permissionMissingLogged = false

        // First enable: start from the current max _ID so history is not backfilled.
        if (storage.callLogLastSentId == null) {
            val maxId = queryMaxCallId() ?: return
            storage.callLogLastSentId = maxId
        }

        val thread = HandlerThread("CallLogReporter").apply { start() }
        val threadHandler = Handler(thread.looper)
        val obs = object : ContentObserver(threadHandler) {
            override fun onChange(selfChange: Boolean) {
                super.onChange(selfChange)
                processNewCalls()
            }
        }

        try {
            context.contentResolver.registerContentObserver(
                CallLog.Calls.CONTENT_URI,
                true,
                obs,
            )
        } catch (e: SecurityException) {
            thread.quitSafely()
            logPermissionMissingOnce()
            return
        }

        handlerThread = thread
        handler = threadHandler
        observer = obs

        logsService.insert(
            LogEntry.Priority.DEBUG,
            MODULE_NAME,
            "Call log observer registered",
        )

        // Catch up rows inserted while the observer was not registered.
        threadHandler.post { processNewCalls() }
    }

    private fun unregister() {
        observer?.let { context.contentResolver.unregisterContentObserver(it) }
        observer = null
        handlerThread?.quitSafely()
        handlerThread = null
        handler = null
    }

    private fun processNewCalls() {
        if (!canReadCallLog()) {
            logPermissionMissingOnce()
            return
        }

        val destination = findDestination() ?: return
        val mark = storage.callLogLastSentId ?: return

        val cursor = try {
            context.contentResolver.query(
                CallLog.Calls.CONTENT_URI,
                arrayOf(
                    CallLog.Calls._ID,
                    CallLog.Calls.NUMBER,
                    CallLog.Calls.TYPE,
                    CallLog.Calls.DURATION,
                    CallLog.Calls.DATE,
                ),
                CallLog.Calls._ID + " > ?",
                arrayOf(mark.toString()),
                CallLog.Calls._ID + " ASC",
            )
        } catch (e: SecurityException) {
            logPermissionMissingOnce()
            return
        } ?: return

        var enqueued = 0
        cursor.use { c ->
            while (c.moveToNext()) {
                val id = c.getLong(0)
                val type = mapType(c.getInt(2))

                if (type != null) {
                    val body = buildBody(
                        deviceId = destination.deviceId,
                        callId = id,
                        number = c.getString(1) ?: "",
                        type = type,
                        durationSec = c.getInt(3),
                        startedAtMillis = c.getLong(4),
                    )

                    try {
                        runBlocking {
                            queueRepository.enqueueWebhook(
                                url = destination.url,
                                payload = body,
                            )
                        }
                    } catch (e: Exception) {
                        // Keep the mark so the row is retried on the next change.
                        logsService.insert(
                            LogEntry.Priority.ERROR,
                            MODULE_NAME,
                            "Failed to enqueue call log event",
                            mapOf("callId" to id, "error" to (e.message ?: e.toString())),
                        )
                        break
                    }

                    enqueued++
                    logsService.insert(
                        LogEntry.Priority.DEBUG,
                        MODULE_NAME,
                        "Call log event enqueued",
                        mapOf("callId" to id, "type" to type),
                    )
                }

                // Advance only after the row was enqueued (or skipped as an unsupported type).
                storage.callLogLastSentId = id
            }
        }

        if (enqueued > 0) {
            WebhookQueueProcessorWorker.start(
                context = context,
                internetRequired = webhooksSettings.internetRequired,
            )
        }
    }

    private fun buildBody(
        deviceId: String,
        callId: Long,
        number: String,
        type: String,
        durationSec: Int,
        startedAtMillis: Long,
    ): String {
        val payload = JsonObject().apply {
            addProperty("callId", callId.toString())
            addProperty("number", number)
            addProperty("type", type)
            addProperty("durationSec", durationSec)
            addProperty("startedAt", gson.toJsonTree(Date(startedAtMillis)).asString)
        }

        return JsonObject().apply {
            addProperty("event", EVENT)
            addProperty("deviceId", deviceId)
            addProperty("id", UUID.randomUUID().toString())
            addProperty("webhookId", WEBHOOK_ID)
            add("payload", payload)
        }.toString()
    }

    /**
     * Finds the locally stored webhook whose URL path ends with `/api/sms/status`
     * and the device id the app puts in webhook bodies for its source.
     */
    private fun findDestination(): Destination? {
        val webhook = webHooksDao.select().firstOrNull { isStatusWebhook(it) }
        if (webhook == null) {
            if (!destinationMissingLogged) {
                destinationMissingLogged = true
                logsService.insert(
                    LogEntry.Priority.INFO,
                    MODULE_NAME,
                    "Call log reporting is off: no webhook ending with $STATUS_PATH is registered",
                )
            }
            return null
        }

        val deviceId = when (webhook.source) {
            EntitySource.Local -> localServerSettings.deviceId
            else -> gatewaySettings.deviceId
        }
        if (deviceId == null) {
            if (!destinationMissingLogged) {
                destinationMissingLogged = true
                logsService.insert(
                    LogEntry.Priority.INFO,
                    MODULE_NAME,
                    "Call log reporting is off: device id is not available",
                )
            }
            return null
        }

        destinationMissingLogged = false
        return Destination(webhook.url, deviceId)
    }

    private fun isStatusWebhook(webhook: WebHook): Boolean {
        val path = try {
            Uri.parse(webhook.url).path
        } catch (e: Exception) {
            null
        } ?: return false

        return path.trimEnd('/').endsWith(STATUS_PATH)
    }

    private fun queryMaxCallId(): Long? {
        val cursor = try {
            context.contentResolver.query(
                CallLog.Calls.CONTENT_URI,
                arrayOf(CallLog.Calls._ID),
                null,
                null,
                CallLog.Calls._ID + " DESC",
            )
        } catch (e: SecurityException) {
            logPermissionMissingOnce()
            return null
        } ?: return null

        return cursor.use { c ->
            if (c.moveToFirst()) c.getLong(0) else 0L
        }
    }

    private fun mapType(type: Int): String? = when (type) {
        CallLog.Calls.INCOMING_TYPE -> "incoming"
        CallLog.Calls.MISSED_TYPE -> "missed"
        CallLog.Calls.OUTGOING_TYPE -> "outgoing"
        CallLog.Calls.REJECTED_TYPE, CallLog.Calls.BLOCKED_TYPE -> "rejected"
        else -> null // voicemail and unknown types are skipped
    }

    private fun canReadCallLog(): Boolean = ContextCompat.checkSelfPermission(
        context,
        Manifest.permission.READ_CALL_LOG,
    ) == PackageManager.PERMISSION_GRANTED

    private fun logPermissionMissingOnce() {
        if (permissionMissingLogged) return
        permissionMissingLogged = true

        logsService.insert(
            LogEntry.Priority.INFO,
            MODULE_NAME,
            "Call log reporting is off: READ_CALL_LOG is not granted",
        )
    }

    private data class Destination(
        val url: String,
        val deviceId: String,
    )

    companion object {
        private const val MODULE_NAME = "calllog"
        private const val EVENT = "call:log"
        private const val WEBHOOK_ID = "calllog"
        private const val STATUS_PATH = "/api/sms/status"

        private val gson = GsonBuilder().configure().create()
    }
}
