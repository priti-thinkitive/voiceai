"""Repository for the `Agents` collection — the only layer touching Motor
for agent data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId

from app.collections import AGENTS
from app.database import MongoDB
from app.models.agent import (
    AgentInDB,
    AgentState,
    AgentStatus,
    AmbientSound,
    CustomToolDefinition,
    DataStorageSetting,
    DenoisingMode,
    HandbookConfig,
    OnHoldMusic,
    PiiConfig,
    PronunciationEntry,
    ResponseEngine,
    StructuredDataFieldDefinition,
    SttMode,
)
from app.models.language import Language

# Fallback defaults for the three transfer-tuning fields on any pre-existing
# document that predates their addition to AgentInDB (added alongside PATCH
# /agents/{agent_id} — see AgentInDB's own docstring for the full "why these
# needed to become persisted fields" reasoning). Deliberately duplicated
# here as literals rather than imported from CreateAgentRequest's own field
# defaults — importing a request model into the repository layer purely to
# borrow its defaults would be a strange, backwards dependency (repository
# depending on the create-time request shape); these three literals ARE
# CreateAgentRequest's real defaults (30000ms, "ringtone", True), just
# named locally so a doc.get() fallback has somewhere to point.
_FALLBACK_TRANSFER_RING_DURATION_MS = 30_000
_FALLBACK_TRANSFER_ON_HOLD_MUSIC = OnHoldMusic.RINGTONE.value
_FALLBACK_TRANSFER_SHOW_ORIGINAL_CALLER_ID = True

# Fallback defaults for the ~19-field tuning-knob batch (see
# app/models/agent.py's module docstring, "~19-field tuning-knob batch"
# section) on any pre-existing document that predates their addition to
# AgentInDB — same doc.get()-with-fallback pattern/reasoning as the three
# transfer-tuning literals above. These ARE CreateAgentRequest's real field
# defaults (which themselves match the voice vendor's own documented
# defaults — see that module's docstring), just named locally so a
# doc.get() fallback has somewhere to point without importing the request
# model into the repository layer.
_FALLBACK_MODEL_TEMPERATURE = 0.0
_FALLBACK_VOICE_TEMPERATURE = 1.0
_FALLBACK_STT_MODE = SttMode.FAST.value
_FALLBACK_DENOISING_MODE = DenoisingMode.NOISE_CANCELLATION.value
_FALLBACK_AMBIENT_SOUND_VOLUME = 1.0
_FALLBACK_BACKCHANNEL_FREQUENCY = 0.8
_FALLBACK_RESPONSIVENESS = 1.0
_FALLBACK_REMINDER_TRIGGER_MS = 10_000
_FALLBACK_REMINDER_MAX_COUNT = 1
_FALLBACK_END_CALL_AFTER_SILENCE_MS = 600_000
_FALLBACK_MAX_CALL_DURATION_MS = 3_600_000
_FALLBACK_BEGIN_MESSAGE_DELAY_MS = 0
_FALLBACK_ALLOW_USER_DTMF = True
_FALLBACK_ALLOW_DTMF_INTERRUPTION = False
_FALLBACK_DATA_STORAGE_SETTING = DataStorageSetting.EVERYTHING.value


def _from_doc(doc: dict[str, Any]) -> AgentInDB:
    return AgentInDB(
        id=str(doc["_id"]),
        platform_id=doc["platform_id"],
        prompt=doc["prompt"],
        voice_id=doc["voice_id"],
        # doc.get()-with-fallback for any pre-existing document that predates
        # the language -> languages rename (there were none in practice — no
        # real data existed before this task shipped, so the single-value
        # "language" key is never actually present on a real document — but
        # this follows the same backfill-less-field-addition pattern already
        # established for updated_at/response_engine above, in case a stray
        # test fixture or future migration needs it).
        languages=[Language(code) for code in doc.get("languages") or [doc["language"]]],
        voice_speed=doc["voice_speed"],
        interruption_sensitivity=doc["interruption_sensitivity"],
        enable_backchannel=doc["enable_backchannel"],
        pronunciation_dictionary=[
            PronunciationEntry(**entry) for entry in doc.get("pronunciation_dictionary", [])
        ],
        # ResponseEngine defaults to BUILTIN for any pre-existing
        # document that predates this field (there were none in practice —
        # no real data existed before this task shipped — but this follows
        # the same doc.get()-with-fallback pattern already established for
        # updated_at, per the standards doc's own guidance on backfill-less
        # field additions).
        response_engine=ResponseEngine(doc.get("response_engine", ResponseEngine.BUILTIN)),
        transfer_number=doc.get("transfer_number"),
        # doc.get()-with-fallback for any pre-existing document that
        # predates these three fields being persisted — see AgentInDB's own
        # docstring and _FALLBACK_* above for why they now need to be
        # stored (not just consumed once at creation time).
        transfer_ring_duration_ms=doc.get(
            "transfer_ring_duration_ms", _FALLBACK_TRANSFER_RING_DURATION_MS
        ),
        transfer_on_hold_music=OnHoldMusic(
            doc.get("transfer_on_hold_music", _FALLBACK_TRANSFER_ON_HOLD_MUSIC)
        ),
        transfer_show_original_caller_id=doc.get(
            "transfer_show_original_caller_id", _FALLBACK_TRANSFER_SHOW_ORIGINAL_CALLER_ID
        ),
        # Defaults to [] for any pre-existing document that predates this
        # field — same doc.get()-with-fallback pattern already established
        # for response_engine/updated_at above (no real data existed before
        # this task shipped, but this follows the project's own documented
        # backfill-less-field-addition convention).
        custom_tools=[CustomToolDefinition(**entry) for entry in doc.get("custom_tools", [])],
        # Defaults to [] for any pre-existing document that predates this
        # field — same doc.get()-with-fallback pattern as custom_tools above.
        structured_data_fields=[
            StructuredDataFieldDefinition(**entry)
            for entry in doc.get("structured_data_fields", [])
        ],
        # states/starting_state default to []/None for any pre-existing
        # document that predates this field — same doc.get()-with-fallback
        # pattern as custom_tools/structured_data_fields above. Persisted
        # from the start (see AgentInDB's own docstring for why), so this
        # fallback only matters for documents written before this feature
        # shipped.
        states=[AgentState(**entry) for entry in doc.get("states", [])],
        starting_state=doc.get("starting_state"),
        # Defaults to None for any pre-existing document that predates this
        # field — same doc.get()-with-fallback pattern as starting_state
        # above. None here means "no welcome_message configured," which is
        # also this field's genuine default state, so no separate sentinel
        # is needed.
        welcome_message=doc.get("welcome_message"),
        # Defaults to None for any pre-existing document that predates this
        # field — same doc.get()-with-fallback pattern as welcome_message
        # above. None here means "no agent_name configured," also this
        # field's genuine default state.
        agent_name=doc.get("agent_name"),
        # Defaults to False for any pre-existing document that predates this
        # field — same doc.get()-with-fallback pattern as agent_name above.
        # False here means "not subscribed to live transcript updates,"
        # also this field's genuine default state (opt-in, see
        # app/models/agent.py's module docstring).
        live_transcript_enabled=doc.get("live_transcript_enabled", False),
        # The ~19-field tuning-knob batch — doc.get()-with-fallback for any
        # pre-existing document that predates their addition, same pattern
        # as every other field above. See app/models/agent.py's module
        # docstring, "~19-field tuning-knob batch" section, for the full
        # feature description of each.
        model=doc.get("model"),
        model_temperature=doc.get("model_temperature", _FALLBACK_MODEL_TEMPERATURE),
        voice_model=doc.get("voice_model"),
        voice_temperature=doc.get("voice_temperature", _FALLBACK_VOICE_TEMPERATURE),
        stt_mode=SttMode(doc.get("stt_mode", _FALLBACK_STT_MODE)),
        denoising_mode=DenoisingMode(doc.get("denoising_mode", _FALLBACK_DENOISING_MODE)),
        ambient_sound=(
            AmbientSound(doc["ambient_sound"]) if doc.get("ambient_sound") is not None else None
        ),
        ambient_sound_volume=doc.get("ambient_sound_volume", _FALLBACK_AMBIENT_SOUND_VOLUME),
        backchannel_frequency=doc.get("backchannel_frequency", _FALLBACK_BACKCHANNEL_FREQUENCY),
        backchannel_words=doc.get("backchannel_words", []),
        responsiveness=doc.get("responsiveness", _FALLBACK_RESPONSIVENESS),
        reminder_trigger_ms=doc.get("reminder_trigger_ms", _FALLBACK_REMINDER_TRIGGER_MS),
        reminder_max_count=doc.get("reminder_max_count", _FALLBACK_REMINDER_MAX_COUNT),
        end_call_after_silence_ms=doc.get(
            "end_call_after_silence_ms", _FALLBACK_END_CALL_AFTER_SILENCE_MS
        ),
        max_call_duration_ms=doc.get("max_call_duration_ms", _FALLBACK_MAX_CALL_DURATION_MS),
        begin_message_delay_ms=doc.get(
            "begin_message_delay_ms", _FALLBACK_BEGIN_MESSAGE_DELAY_MS
        ),
        allow_user_dtmf=doc.get("allow_user_dtmf", _FALLBACK_ALLOW_USER_DTMF),
        allow_dtmf_interruption=doc.get(
            "allow_dtmf_interruption", _FALLBACK_ALLOW_DTMF_INTERRUPTION
        ),
        data_storage_setting=DataStorageSetting(
            doc.get("data_storage_setting", _FALLBACK_DATA_STORAGE_SETTING)
        ),
        pii_config=(PiiConfig(**doc["pii_config"]) if doc.get("pii_config") is not None else None),
        post_call_analysis_model=doc.get("post_call_analysis_model"),
        handbook_config=(
            HandbookConfig(**doc["handbook_config"])
            if doc.get("handbook_config") is not None
            else None
        ),
        status=AgentStatus(doc["status"]),
        vendor=doc["vendor"],
        vendor_ref=doc.get("vendor_ref"),
        llm_ref=doc.get("llm_ref"),
        created_at=doc["created_at"],
        updated_at=doc.get("updated_at", doc["created_at"]),
    )


async def create(
    db: MongoDB,
    *,
    platform_id: str,
    prompt: str,
    voice_id: str,
    languages: list[Language],
    voice_speed: float,
    interruption_sensitivity: float,
    enable_backchannel: bool,
    pronunciation_dictionary: list[PronunciationEntry],
    status: AgentStatus,
    vendor: str,
    vendor_ref: str | None,
    # Defaulted (not required) deliberately: most call sites across this
    # codebase's test suite create an agent only as a prerequisite for
    # testing something else entirely (a phone number, a call, a webhook) —
    # forcing every one of those unrelated call sites to also reason about
    # response_engine/transfer_number would be real, pointless friction for
    # a concern outside their scope. The router (app/routers/agents.py),
    # which IS the real feature surface for this, always passes both
    # explicitly. Defaults mirror CreateAgentRequest's own defaults
    # (builtin, no transfer configured) for consistency.
    response_engine: ResponseEngine = ResponseEngine.BUILTIN,
    transfer_number: str | None = None,
    transfer_ring_duration_ms: int = _FALLBACK_TRANSFER_RING_DURATION_MS,
    transfer_on_hold_music: OnHoldMusic = OnHoldMusic.RINGTONE,
    transfer_show_original_caller_id: bool = _FALLBACK_TRANSFER_SHOW_ORIGINAL_CALLER_ID,
    custom_tools: list[CustomToolDefinition] | None = None,
    structured_data_fields: list[StructuredDataFieldDefinition] | None = None,
    states: list[AgentState] | None = None,
    starting_state: str | None = None,
    welcome_message: str | None = None,
    agent_name: str | None = None,
    live_transcript_enabled: bool = False,
    llm_ref: str | None = None,
    # The ~19-field tuning-knob batch — defaulted the same way as every
    # other field above, mirroring CreateAgentRequest's own defaults (which
    # themselves match the voice vendor's own documented defaults — see
    # app/models/agent.py's module docstring, "~19-field tuning-knob batch"
    # section).
    model: str | None = None,
    model_temperature: float = _FALLBACK_MODEL_TEMPERATURE,
    voice_model: str | None = None,
    voice_temperature: float = _FALLBACK_VOICE_TEMPERATURE,
    stt_mode: SttMode = SttMode.FAST,
    denoising_mode: DenoisingMode = DenoisingMode.NOISE_CANCELLATION,
    ambient_sound: AmbientSound | None = None,
    ambient_sound_volume: float = _FALLBACK_AMBIENT_SOUND_VOLUME,
    backchannel_frequency: float = _FALLBACK_BACKCHANNEL_FREQUENCY,
    backchannel_words: list[str] | None = None,
    responsiveness: float = _FALLBACK_RESPONSIVENESS,
    reminder_trigger_ms: int = _FALLBACK_REMINDER_TRIGGER_MS,
    reminder_max_count: int = _FALLBACK_REMINDER_MAX_COUNT,
    end_call_after_silence_ms: int = _FALLBACK_END_CALL_AFTER_SILENCE_MS,
    max_call_duration_ms: int = _FALLBACK_MAX_CALL_DURATION_MS,
    begin_message_delay_ms: int = _FALLBACK_BEGIN_MESSAGE_DELAY_MS,
    allow_user_dtmf: bool = _FALLBACK_ALLOW_USER_DTMF,
    allow_dtmf_interruption: bool = _FALLBACK_ALLOW_DTMF_INTERRUPTION,
    data_storage_setting: DataStorageSetting = DataStorageSetting.EVERYTHING,
    pii_config: PiiConfig | None = None,
    post_call_analysis_model: str | None = None,
    handbook_config: HandbookConfig | None = None,
) -> AgentInDB:
    now = datetime.now(UTC)
    doc = {
        "platform_id": platform_id,
        "prompt": prompt,
        "voice_id": voice_id,
        "languages": [lang.value for lang in languages],
        "voice_speed": voice_speed,
        "interruption_sensitivity": interruption_sensitivity,
        "enable_backchannel": enable_backchannel,
        "pronunciation_dictionary": [entry.model_dump() for entry in pronunciation_dictionary],
        "response_engine": response_engine.value,
        "transfer_number": transfer_number,
        "transfer_ring_duration_ms": transfer_ring_duration_ms,
        "transfer_on_hold_music": transfer_on_hold_music.value,
        "transfer_show_original_caller_id": transfer_show_original_caller_id,
        "custom_tools": [tool.model_dump(mode="json") for tool in (custom_tools or [])],
        "structured_data_fields": [
            field.model_dump(mode="json") for field in (structured_data_fields or [])
        ],
        "states": [state.model_dump(mode="json") for state in (states or [])],
        "starting_state": starting_state,
        "welcome_message": welcome_message,
        "agent_name": agent_name,
        "live_transcript_enabled": live_transcript_enabled,
        "model": model,
        "model_temperature": model_temperature,
        "voice_model": voice_model,
        "voice_temperature": voice_temperature,
        "stt_mode": stt_mode.value,
        "denoising_mode": denoising_mode.value,
        "ambient_sound": ambient_sound.value if ambient_sound is not None else None,
        "ambient_sound_volume": ambient_sound_volume,
        "backchannel_frequency": backchannel_frequency,
        "backchannel_words": backchannel_words or [],
        "responsiveness": responsiveness,
        "reminder_trigger_ms": reminder_trigger_ms,
        "reminder_max_count": reminder_max_count,
        "end_call_after_silence_ms": end_call_after_silence_ms,
        "max_call_duration_ms": max_call_duration_ms,
        "begin_message_delay_ms": begin_message_delay_ms,
        "allow_user_dtmf": allow_user_dtmf,
        "allow_dtmf_interruption": allow_dtmf_interruption,
        "data_storage_setting": data_storage_setting.value,
        "pii_config": pii_config.model_dump(mode="json") if pii_config is not None else None,
        "post_call_analysis_model": post_call_analysis_model,
        "handbook_config": (
            handbook_config.model_dump(mode="json") if handbook_config is not None else None
        ),
        "status": status.value,
        "vendor": vendor,
        "vendor_ref": vendor_ref,
        "llm_ref": llm_ref,
        "created_at": now,
        "updated_at": now,
    }
    result = await db[AGENTS].insert_one(doc)
    doc["_id"] = result.inserted_id
    return _from_doc(doc)


async def get_by_id(db: MongoDB, agent_id: str, *, platform_id: str) -> AgentInDB | None:
    """Tenancy-scoped lookup — always filters on platform_id, never trusts a
    caller-supplied identifier alone. Mirrors platform_repo's ObjectId guard.
    """
    if not ObjectId.is_valid(agent_id):
        return None
    doc = await db[AGENTS].find_one({"_id": ObjectId(agent_id), "platform_id": platform_id})
    return _from_doc(doc) if doc else None


async def update(
    db: MongoDB,
    agent_id: str,
    *,
    platform_id: str,
    prompt: str,
    voice_id: str,
    languages: list[Language],
    voice_speed: float,
    interruption_sensitivity: float,
    enable_backchannel: bool,
    pronunciation_dictionary: list[PronunciationEntry],
    transfer_number: str | None,
    transfer_ring_duration_ms: int,
    transfer_on_hold_music: OnHoldMusic,
    transfer_show_original_caller_id: bool,
    custom_tools: list[CustomToolDefinition],
    structured_data_fields: list[StructuredDataFieldDefinition],
    states: list[AgentState],
    starting_state: str | None,
    welcome_message: str | None,
    agent_name: str | None,
    live_transcript_enabled: bool,
    model: str | None,
    model_temperature: float,
    voice_model: str | None,
    voice_temperature: float,
    stt_mode: SttMode,
    denoising_mode: DenoisingMode,
    ambient_sound: AmbientSound | None,
    ambient_sound_volume: float,
    backchannel_frequency: float,
    backchannel_words: list[str],
    responsiveness: float,
    reminder_trigger_ms: int,
    reminder_max_count: int,
    end_call_after_silence_ms: int,
    max_call_duration_ms: int,
    begin_message_delay_ms: int,
    allow_user_dtmf: bool,
    allow_dtmf_interruption: bool,
    data_storage_setting: DataStorageSetting,
    pii_config: PiiConfig | None,
    post_call_analysis_model: str | None,
    handbook_config: HandbookConfig | None,
) -> bool:
    """Persist the FULLY MERGED post-update state — every argument here is
    the final value that should end up stored, already resolved by the
    router (app/routers/agents.py's `update_agent`) from "existing stored
    value" + "whichever fields the caller's UpdateAgentRequest actually
    set." This function does not itself know or care which fields were
    "changed" vs "left alone" — that distinction only matters upstream, at
    the request-merging step; by the time a value reaches here, it is simply
    the record's new state, same as every field on `create()` above.

    Deliberately does NOT accept `response_engine`, `status`, `vendor`,
    `vendor_ref`, or `llm_ref` as parameters — none of those can change via
    this update path. `response_engine` cannot change at all (see
    `UpdateAgentRequest`'s docstring for the full reasoning on why mode-
    switching is out of scope for this first pass); `status`/`vendor_ref`/
    `llm_ref` are outcomes of whether the vendor call(s) this update makes
    succeed or fail, which the router reasons about and may choose to leave
    unchanged (a partial-failure update still keeps the agent "active" with
    its EXISTING vendor_ref/llm_ref, since those vendor-side objects still
    exist and still work — only the specific field(s) that failed to update
    are out of sync — see the router's own docstring for the full partial-
    failure contract) — this function has no opinion on that decision, it
    only ever writes what the router tells it to.

    Returns a bool (`modified_count == 1`), same "thin, explicit write path"
    convention as platform_repo's `set_inbound_variables_webhook_url`/
    `set_call_completed_webhook_url` — the caller (router) does its own
    fresh `get_by_id()` afterward to read back the persisted record for the
    response, rather than this function trying to hand back a document
    itself. `False` covers both "agent_id malformed" and "no matching
    document for this platform_id" (same tenancy-scoping as every other
    function in this module) — the router already did its own `get_by_id`
    lookup before calling this (to read the CURRENT state needed for the
    merge in the first place), so by the time this is called the agent's
    existence/ownership is already established; a `False` here would only
    happen from a genuinely concurrent delete, which this codebase has no
    delete-agent endpoint to cause today.
    """
    if not ObjectId.is_valid(agent_id):
        return False
    now = datetime.now(UTC)
    result = await db[AGENTS].update_one(
        {"_id": ObjectId(agent_id), "platform_id": platform_id},
        {
            "$set": {
                "prompt": prompt,
                "voice_id": voice_id,
                "languages": [lang.value for lang in languages],
                "voice_speed": voice_speed,
                "interruption_sensitivity": interruption_sensitivity,
                "enable_backchannel": enable_backchannel,
                "pronunciation_dictionary": [
                    entry.model_dump() for entry in pronunciation_dictionary
                ],
                "transfer_number": transfer_number,
                "transfer_ring_duration_ms": transfer_ring_duration_ms,
                "transfer_on_hold_music": transfer_on_hold_music.value,
                "transfer_show_original_caller_id": transfer_show_original_caller_id,
                "custom_tools": [tool.model_dump(mode="json") for tool in custom_tools],
                "structured_data_fields": [
                    field.model_dump(mode="json") for field in structured_data_fields
                ],
                "states": [state.model_dump(mode="json") for state in states],
                "starting_state": starting_state,
                "welcome_message": welcome_message,
                "agent_name": agent_name,
                "live_transcript_enabled": live_transcript_enabled,
                "model": model,
                "model_temperature": model_temperature,
                "voice_model": voice_model,
                "voice_temperature": voice_temperature,
                "stt_mode": stt_mode.value,
                "denoising_mode": denoising_mode.value,
                "ambient_sound": ambient_sound.value if ambient_sound is not None else None,
                "ambient_sound_volume": ambient_sound_volume,
                "backchannel_frequency": backchannel_frequency,
                "backchannel_words": backchannel_words,
                "responsiveness": responsiveness,
                "reminder_trigger_ms": reminder_trigger_ms,
                "reminder_max_count": reminder_max_count,
                "end_call_after_silence_ms": end_call_after_silence_ms,
                "max_call_duration_ms": max_call_duration_ms,
                "begin_message_delay_ms": begin_message_delay_ms,
                "allow_user_dtmf": allow_user_dtmf,
                "allow_dtmf_interruption": allow_dtmf_interruption,
                "data_storage_setting": data_storage_setting.value,
                "pii_config": (
                    pii_config.model_dump(mode="json") if pii_config is not None else None
                ),
                "post_call_analysis_model": post_call_analysis_model,
                "handbook_config": (
                    handbook_config.model_dump(mode="json")
                    if handbook_config is not None
                    else None
                ),
                "updated_at": now,
            }
        },
    )
    return result.modified_count == 1


async def get_by_vendor_ref(db: MongoDB, vendor_ref: str) -> AgentInDB | None:
    """Lookup by the voice vendor's own agent id, with NO platform_id
    filter — deliberately different from get_by_id above. Used by two
    vendor-webhook handlers in app/routers/webhooks.py: the custom-tool
    proxy (to resolve which of our own agents, and therefore which platform
    and which tool's registered webhook_url, a real vendor tool-call webhook
    concerns) and the post-call handler (to resolve which agent/platform
    owns a genuinely inbound call it's never seen before, so it can create
    the missing Calls document — see that handler's docstring).

    Same "tenancy gets resolved BY this lookup, so it cannot itself be
    tenancy-scoped" reasoning already documented on
    phone_number_repo.get_by_phone_number_any_platform() and
    call_repo.get_by_vendor_ref() for the other two vendor-webhook-driven
    lookups in this codebase. Safe for the same reason: the caller here is
    the voice vendor's own HMAC-signature-verified webhook, not an arbitrary
    HTTP caller, and this returns at most the one Agents document that
    genuinely has this vendor_ref (set only by our own POST /agents at
    creation time), never platform-scoped data belonging to someone else
    beyond that single already-correlated record.
    """
    doc = await db[AGENTS].find_one({"vendor_ref": vendor_ref})
    return _from_doc(doc) if doc else None
