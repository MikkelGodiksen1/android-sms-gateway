package me.capcom.smsgateway.modules.receiver

import me.capcom.smsgateway.modules.settings.KeyValueStorage
import me.capcom.smsgateway.modules.settings.get

class StateStorage(
    private val storage: KeyValueStorage,
) {
    var mmsLastProcessedID: Long
        get() = storage.get<Long>(MMS_LAST_PROCESSED_ID) ?: 0
        set(value) = storage.set(MMS_LAST_PROCESSED_ID, value)

    var smsLastProcessedID: Long
        get() = storage.get<Long>(SMS_LAST_PROCESSED_ID) ?: 0
        set(value) = storage.set(SMS_LAST_PROCESSED_ID, value)

    /**
     * Highest call log `_ID` handed to the webhook queue. Null until call log reporting
     * is enabled for the first time.
     */
    var callLogLastSentId: Long?
        get() = storage.get<Long>(CALL_LOG_LAST_SENT_ID)
        set(value) = storage.set(CALL_LOG_LAST_SENT_ID, value)

    companion object {
        private val PREFIX = "state."

        private val MMS_LAST_PROCESSED_ID = PREFIX + "last_processed_id"
        private val SMS_LAST_PROCESSED_ID = PREFIX + "sms_last_processed_id"
        private val CALL_LOG_LAST_SENT_ID = PREFIX + "call_log_last_sent_id"
    }
}