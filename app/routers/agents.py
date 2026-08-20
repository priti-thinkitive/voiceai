"""POST /agents — Phase 1 feature 1: create a voice agent for a
platform-client (one base prompt, one voice), per
vendor-docs/White-Label-Launch-Plan.html's Phase 1 scope items 1, 2, 3, 15.
Also closes the Phase 1 "transfer to a human" gap (see
app/models/agent.py's module docstring for the full response_engine/
transfer_number design) — a `builtin`-mode agent (the default)
supports real warm transfer today; see CreateAgentRequest for the request
shape.

Also POST /agents/{agent_id}/numbers (buy-new, Option 1 of telephony) and
POST /agents/{agent_id}/numbers/byo (bring-your-own SIP trunk, Option 2) —
Phase 1 item 4, both siblings that bind a phone number to an agent, either
purchased through Retell or imported from Platform X's own SIP trunk.

**PATCH /agents/{agent_id} — NEW, closes a real, confirmed gap: until now
there was no way to change anything about an existing agent (prompt,
transfer number, custom tools, tuning) short of creating an entirely new
one.** See `UpdateAgentRequest`'s module-level docstring (app/models/
agent.py) for the full partial-update semantics/design reasoning — this
docstring only covers what's specific to the router/vendor-call layer:
sequencing, tenancy, and partial-failure handling.

**Vendor call sequencing.** A `builtin`-mode agent (the only mode with a
real `llm_ref`) may need up to two real vendor calls for one PATCH: `PATCH
/update-agent/{vendor_ref}` for agent-object fields (voice_id, language,
voice_speed, interruption_sensitivity, enable_backchannel,
pronunciation_dictionary, structured_data_fields/post_call_analysis_data,
agent_name, live_transcript_enabled/webhook_events), and `PATCH
/update-retell-llm/{llm_ref}` for LLM-object fields (general_prompt,
general_tools i.e. transfer_number/custom_tools, begin_message/
welcome_message, states). A `custom`-mode agent only ever needs the first —
it has no `llm_ref` at all (see AgentInDB's docstring) — but that first call
still covers `agent_name`/`live_transcript_enabled`, unlike every other
field just listed under it, since both live on the agent object, not the LLM
object, and are therefore available on a `custom`-mode agent too (see
app/models/agent.py's module docstring, "agent_name"/"live_transcript_
enabled" sections). Both calls, when both are needed, are attempted
regardless of whether the fields relevant to each actually appear in the
incoming request — this keeps the sequencing simple and matches how
`create_retell_llm_agent()` already always makes both of its calls rather
than conditionally skipping one based on which CreateAgentRequest fields
were set.

**Partial-failure handling between the two vendor calls, decided
explicitly.** If `update-agent` succeeds but `update-retell-llm` fails (or
vice versa), the vendor is now in a state where SOME of the agent's fields
changed and others didn't — an honest partial success, not a clean failure
we can roll back (Retell has no transactional multi-object update, same
fundamental constraint `create_retell_llm_agent()`'s orphan-cleanup already
works around for creation). This router's answer: **persist on our own side
exactly what we know actually reached the vendor, not what the caller
asked for.** Concretely — the merged, INTENDED post-update state is computed
once up front; each vendor call, independently, either succeeds (its half of
that intended state is now real on the vendor) or fails (its half never
took effect, so the PREVIOUS stored value for exactly those fields is kept
instead, not the caller's newly-requested one). Our own stored Agent
document is then updated to reflect this actual mixed outcome — never the
caller's full request unconditionally, since that would silently claim a
field changed when the vendor call that would have changed it never
succeeded. The response mirrors this same mixed state via `_to_public()`,
and if either vendor call failed, the endpoint still returns the real
`upstream_failed`/502 contract (never a silent 200 hiding a partial
failure) — but the vendor-successful half of the change is not thrown away
or rolled back merely because its sibling call failed, matching this
codebase's existing "never leave a caller worse off than a clean retry
would" spirit (see create_retell_llm_agent()'s orphan-cleanup docstring for
the same value applied to the creation path, and POST /agents' "persist
regardless of vendor outcome" precedent above).

**Mode-switching (response_engine) is rejected, not silently ignored or
attempted** — `UpdateAgentRequest` has no `response_engine` field at all
(see that model's docstring for the full reasoning) and sets
`extra="forbid"` specifically so a caller who sends `response_engine` (or
any other unrecognized field) gets an explicit, loud 422 naming the unknown
field, rather than Pydantic's own default `extra="ignore"` behavior every
other model in this codebase otherwise relies on quietly discarding it and
returning a misleadingly-normal 200.

**Tenancy/SSRF**: identical patterns to every other agent-scoped endpoint
in this router — `agent_repo.get_by_id(db, agent_id, platform_id=caller.id)`
(cross-platform lookup 404s, never 403) and `reject_if_internal_url` on any
newly-supplied `custom_tools[].webhook_url`, both applied before any vendor
call is attempted.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, status

from app.config import Settings, get_settings
from app.deps import CurrentPlatform, DbDep
from app.errors import CODE_VALIDATION, AppError
from app.models.agent import (
    AgentInDB,
    AgentPublic,
    AgentStatus,
    CreateAgentRequest,
    ResponseEngine,
    UpdateAgentRequest,
)
from app.models.phone_number import (
    CreatePhoneNumberRequest,
    ImportPhoneNumberRequest,
    PhoneNumberInDB,
    PhoneNumberPublic,
)
from app.repositories import agent_repo, phone_number_repo
from app.services import retell_adapter, retell_agent_adapter
from app.utils.ssrf_guard import reject_if_internal_url

router = APIRouter(prefix="/agents", tags=["agents"])


def _to_public_number(number: PhoneNumberInDB) -> PhoneNumberPublic:
    return PhoneNumberPublic(
        phone_number=number.phone_number,
        area_code=number.area_code,
        nickname=number.nickname,
        agent_id=number.agent_id,
        created_at=number.created_at,
    )


def _to_public(agent: AgentInDB) -> AgentPublic:
    return AgentPublic(
        id=agent.id,
        platform_id=agent.platform_id,
        prompt=agent.prompt,
        voice_id=agent.voice_id,
        languages=agent.languages,
        voice_speed=agent.voice_speed,
        interruption_sensitivity=agent.interruption_sensitivity,
        enable_backchannel=agent.enable_backchannel,
        pronunciation_dictionary=agent.pronunciation_dictionary,
        response_engine=agent.response_engine,
        # Derived, not stored — see AgentPublic's docstring for why this is
        # computed here rather than persisted as its own field.
        transfer_enabled=(
            agent.response_engine == ResponseEngine.BUILTIN
            and agent.transfer_number is not None
        ),
        transfer_ring_duration_ms=agent.transfer_ring_duration_ms,
        transfer_on_hold_music=agent.transfer_on_hold_music,
        transfer_show_original_caller_id=agent.transfer_show_original_caller_id,
        custom_tools=agent.custom_tools,
        structured_data_fields=agent.structured_data_fields,
        states=agent.states,
        starting_state=agent.starting_state,
        # Derived, not stored — same "at-a-glance answer" reasoning as
        # transfer_enabled above (see AgentPublic's docstring).
        multi_prompt_enabled=len(agent.states) > 0,
        welcome_message=agent.welcome_message,
        agent_name=agent.agent_name,
        live_transcript_enabled=agent.live_transcript_enabled,
        model=agent.model,
        model_temperature=agent.model_temperature,
        voice_model=agent.voice_model,
        voice_temperature=agent.voice_temperature,
        stt_mode=agent.stt_mode,
        denoising_mode=agent.denoising_mode,
        ambient_sound=agent.ambient_sound,
        ambient_sound_volume=agent.ambient_sound_volume,
        backchannel_frequency=agent.backchannel_frequency,
        backchannel_words=agent.backchannel_words,
        responsiveness=agent.responsiveness,
        reminder_trigger_ms=agent.reminder_trigger_ms,
        reminder_max_count=agent.reminder_max_count,
        end_call_after_silence_ms=agent.end_call_after_silence_ms,
        max_call_duration_ms=agent.max_call_duration_ms,
        begin_message_delay_ms=agent.begin_message_delay_ms,
        allow_user_dtmf=agent.allow_user_dtmf,
        allow_dtmf_interruption=agent.allow_dtmf_interruption,
        data_storage_setting=agent.data_storage_setting,
        pii_config=agent.pii_config,
        post_call_analysis_model=agent.post_call_analysis_model,
        handbook_config=agent.handbook_config,
        status=agent.status,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _reject_transfer_fields_under_update(
    body: UpdateAgentRequest, *, current_response_engine: ResponseEngine
) -> None:
    """Same restriction, same reasoning, as CreateAgentRequest's own
    `_reject_transfer_fields_under_custom_mode` validator (app/models/
    agent.py) — a 'custom'-mode agent has no vendor-side general_tools
    mechanism to attach transfer/custom tools/Multi Prompt states to, so
    setting any of the three via PATCH is exactly as meaningless as setting
    them at creation time, and gets the identical loud-422-not-silent-drop
    treatment.

    Lives in the ROUTER, not as a model validator on UpdateAgentRequest
    itself, for one unavoidable reason: `UpdateAgentRequest` has no
    `response_engine` field (mode-switching is out of scope for this PATCH
    entirely — see that model's docstring), so which mode is "current" is
    only known once the existing AgentInDB has been fetched, which only the
    router can do. A model-level `@model_validator` only ever sees the
    request body in isolation, never the record it's updating — this check
    genuinely cannot live where CreateAgentRequest's sibling validator does.

    Checks `clear_transfer_number` too, not just a non-None
    `transfer_number` — a caller trying to explicitly clear a
    transfer_number on an agent that's already 'custom' mode is a
    harmless no-op in practice (there's nothing to clear, since 'custom'
    agents can never have a transfer_number in the first place), so this
    does NOT reject that case; only a genuine attempt to SET one, or to set
    a non-empty custom_tools/states, is rejected. `welcome_message` follows
    the identical "don't reject a harmless clear" carve-out —
    `clear_welcome_message` alone is never rejected here, only a genuine
    attempt to SET `welcome_message` (checked via `is not None`, since ""
    is a real, meaningful value here too — see this field's own docstring
    in app/models/agent.py).

    **`agent_name` is DELIBERATELY not checked here at all — not even the
    "harmless clear" carve-out, since there is no restriction to carve out
    of.** Unlike `transfer_number`/`custom_tools`/`states`/`welcome_message`
    above, `agent_name` lives on the vendor's agent object, not its LLM
    object, so it is genuinely valid to set/clear on a 'custom'-mode agent
    exactly the same as a 'builtin' one — see app/models/agent.py's module
    docstring, "agent_name" section, for the full agent-object-vs-LLM-object
    placement reasoning. A future maintainer extending this function to
    cover a new field should re-read that section before assuming every
    field on `UpdateAgentRequest` needs the same treatment.

    **`live_transcript_enabled` is the SAME kind of exception as
    `agent_name`, for the identical reason — also deliberately not checked
    here.** It lives on the vendor's agent object (`webhook_events`), not
    its LLM object, so it is genuinely valid to set on a 'custom'-mode agent
    exactly the same as a 'builtin' one — see app/models/agent.py's module
    docstring, "live_transcript_enabled" section.
    """
    if current_response_engine == ResponseEngine.CUSTOM:
        if body.transfer_number is not None:
            raise AppError(
                code=CODE_VALIDATION,
                message="transfer_number can only be set on a 'builtin' agent — this agent's "
                "response_engine is 'custom', which cannot support transfer at all. "
                "response_engine cannot be changed via this endpoint; create a new agent if "
                "you need transfer support.",
                status_code=422,
                field="transfer_number",
            )
        if body.custom_tools:
            raise AppError(
                code=CODE_VALIDATION,
                message="custom_tools can only be set on a 'builtin' agent — this agent's "
                "response_engine is 'custom', which has no vendor-side tool-registration "
                "mechanism to attach them to. response_engine cannot be changed via this "
                "endpoint; create a new agent if you need custom tools.",
                status_code=422,
                field="custom_tools",
            )
        if body.states:
            raise AppError(
                code=CODE_VALIDATION,
                message="states can only be set on a 'builtin' agent — this agent's "
                "response_engine is 'custom', which has no vendor-side conversation-brain "
                "object to attach Multi Prompt states to. response_engine cannot be changed "
                "via this endpoint; create a new agent if you need Multi Prompt.",
                status_code=422,
                field="states",
            )
        if body.welcome_message is not None:
            raise AppError(
                code=CODE_VALIDATION,
                message="welcome_message can only be set on a 'builtin' agent — this agent's "
                "response_engine is 'custom', which has no vendor-side conversation-brain "
                "object to attach a fixed welcome_message to. response_engine cannot be "
                "changed via this endpoint; create a new agent if you need welcome_message.",
                status_code=422,
                field="welcome_message",
            )
        if body.model is not None:
            raise AppError(
                code=CODE_VALIDATION,
                message="model can only be set on a 'builtin' agent — this agent's "
                "response_engine is 'custom', which has no vendor-side conversation-brain "
                "object to attach a model choice to. response_engine cannot be changed via "
                "this endpoint; create a new agent if you need to choose a model.",
                status_code=422,
                field="model",
            )
        # model_temperature and the rest of the ~19-field tuning-knob batch
        # (voice_model through handbook_config) are deliberately NOT checked
        # here — model_temperature has no None-means-"not set" state to
        # signal real intent with (same reasoning as
        # CreateAgentRequest's own _reject_transfer_fields_under_custom_mode
        # validator), and every other field in that batch is an agent-object
        # field genuinely valid under BOTH response_engine values, same as
        # agent_name/live_transcript_enabled just above — see
        # app/models/agent.py's module docstring, "~19-field tuning-knob
        # batch" section, for the full placement confirmation. A future
        # maintainer extending this function should re-read that section
        # before assuming every new field needs the same treatment.


@router.post(
    "",
    response_model=AgentPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Create a voice agent (one base prompt, one voice, optional transfer to a human)",
    responses={
        422: {
            "description": "Request validation failed — includes transfer_number being set "
            "alongside response_engine='custom', which cannot support transfer at "
            "all.",
        },
        502: {
            "description": "The voice vendor could not be reached or rejected the request. "
            "The agent is still created on our side with status 'failed' so it can be "
            "retried without re-submitting the prompt/voice/tuning/transfer fields.",
        },
    },
)
async def create_agent(
    body: CreateAgentRequest,
    caller: CurrentPlatform,
    db: DbDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> AgentPublic:
    """Create a voice agent for the calling platform.

    Only `prompt` and `voice_id` are required; every audio-tuning field
    (`voice_speed`, `interruption_sensitivity`, `enable_backchannel`,
    `pronunciation_dictionary`) and `language` default to sensible values if
    omitted — see CreateAgentRequest for exact defaults/ranges.

    `response_engine` (default `builtin`) picks which conversation
    brain the agent uses — see CreateAgentRequest's docstring and Field
    description for the full, honest breakdown of what each mode can
    actually do today. `builtin` mode makes two real vendor calls in
    sequence (set up the conversation brain, then create the agent
    referencing it); `custom` mode makes one. `transfer_number`
    (and its tuning fields) are only meaningful under `builtin` —
    CreateAgentRequest itself already rejects them with 422 if set alongside
    `custom`, so this handler never has to re-check that here.

    We persist our own agent record regardless of whether the call(s) to the
    voice vendor succeed. From Platform X's side, the record they asked us
    to create (prompt, voice, tuning, transfer config) is real and theirs
    the moment we accept the request — a vendor outage on our backend
    shouldn't force them to re-submit the same payload and re-solve a
    transient error themselves. If the vendor call fails, the agent is
    stored with status "failed" (no vendor_ref/llm_ref yet) rather than
    silently discarded, so a future retry/repair path has a stable id to act
    on. If the vendor call succeeds, status is "active" and vendor_ref/
    llm_ref (both internal-only) are recorded for webhook correlation and
    support debugging.

    Each `custom_tools` entry's own `webhook_url` — and, for a Multi Prompt
    agent, each `states[].tools[]` entry's own `webhook_url` too, same
    shape/same risk — gets the same SSRF-adjacent check already built for
    `inbound_variables_webhook_url`/`call_completed_webhook_url`
    (app/utils/ssrf_guard.py) — it is an address our own server will later
    POST to automatically, mid-call, on Platform X's behalf, the identical
    risk class as those two fields. This is checked before any vendor call
    is attempted, same "reject up front" discipline as every other
    request-level validation in this handler.
    """
    for tool in body.custom_tools:
        await reject_if_internal_url(tool.webhook_url, field="custom_tools.webhook_url")
    for state in body.states:
        for tool in state.tools:
            await reject_if_internal_url(tool.webhook_url, field="states.tools.webhook_url")

    status_value = AgentStatus.ACTIVE
    vendor_ref: str | None = None
    llm_ref: str | None = None
    vendor_error: AppError | None = None
    webhook_url = f"{settings.BASE_URL}/webhooks/retell/post-call"

    try:
        if body.response_engine == ResponseEngine.BUILTIN:
            builtin_result = await retell_agent_adapter.create_retell_llm_agent(
                settings,
                body=body,
                voice_id=body.voice_id,
                languages=body.languages_list,
                voice_speed=body.voice_speed,
                interruption_sensitivity=body.interruption_sensitivity,
                enable_backchannel=body.enable_backchannel,
                pronunciation_dictionary=body.pronunciation_dictionary,
                webhook_url=webhook_url,
                structured_data_fields=body.structured_data_fields,
            )
            vendor_ref = builtin_result.agent_id
            llm_ref = builtin_result.llm_id
        else:
            custom_result = await retell_agent_adapter.create_agent(
                settings,
                prompt=body.prompt,
                voice_id=body.voice_id,
                languages=body.languages_list,
                voice_speed=body.voice_speed,
                interruption_sensitivity=body.interruption_sensitivity,
                enable_backchannel=body.enable_backchannel,
                pronunciation_dictionary=body.pronunciation_dictionary,
                # Without this, the voice vendor has nowhere to send
                # call_started/call_ended/call_analyzed events for this
                # agent's calls and POST /webhooks/retell/post-call never
                # fires — see app/routers/webhooks.py's post-call handler
                # docstring.
                webhook_url=webhook_url,
                structured_data_fields=body.structured_data_fields,
                # agent_name is an agent-object field, available under
                # 'custom' mode too — see app/models/agent.py's module
                # docstring, "agent_name" section.
                agent_name=body.agent_name,
                # live_transcript_enabled is an agent-object field too, same
                # placement as agent_name — see app/models/agent.py's module
                # docstring, "live_transcript_enabled" section.
                live_transcript_enabled=body.live_transcript_enabled,
                # The rest of the ~19-field tuning-knob batch (voice_model
                # through handbook_config) is all agent-object, available
                # under 'custom' mode too — model/model_temperature are the
                # only two LLM-object fields in the batch, and both are
                # rejected under 'custom' mode by CreateAgentRequest's own
                # validator, so they're never relevant on this path.
                agent_tuning_fields=retell_agent_adapter._build_agent_tuning_fields(
                    voice_model=body.voice_model,
                    voice_temperature=body.voice_temperature,
                    stt_mode=body.stt_mode,
                    denoising_mode=body.denoising_mode,
                    ambient_sound=body.ambient_sound,
                    ambient_sound_volume=body.ambient_sound_volume,
                    backchannel_frequency=body.backchannel_frequency,
                    backchannel_words=body.backchannel_words,
                    responsiveness=body.responsiveness,
                    reminder_trigger_ms=body.reminder_trigger_ms,
                    reminder_max_count=body.reminder_max_count,
                    end_call_after_silence_ms=body.end_call_after_silence_ms,
                    max_call_duration_ms=body.max_call_duration_ms,
                    begin_message_delay_ms=body.begin_message_delay_ms,
                    allow_user_dtmf=body.allow_user_dtmf,
                    allow_dtmf_interruption=body.allow_dtmf_interruption,
                    data_storage_setting=body.data_storage_setting,
                    pii_config=body.pii_config,
                    post_call_analysis_model=body.post_call_analysis_model,
                    handbook_config=body.handbook_config,
                ),
            )
            vendor_ref = custom_result.agent_id
    except AppError as exc:
        status_value = AgentStatus.FAILED
        vendor_error = exc

    agent = await agent_repo.create(
        db,
        platform_id=caller.id,
        prompt=body.prompt,
        voice_id=body.voice_id,
        languages=body.languages_list,
        voice_speed=body.voice_speed,
        interruption_sensitivity=body.interruption_sensitivity,
        enable_backchannel=body.enable_backchannel,
        pronunciation_dictionary=body.pronunciation_dictionary,
        response_engine=body.response_engine,
        transfer_number=body.transfer_number,
        transfer_ring_duration_ms=body.transfer_ring_duration_ms,
        transfer_on_hold_music=body.transfer_on_hold_music,
        transfer_show_original_caller_id=body.transfer_show_original_caller_id,
        custom_tools=body.custom_tools,
        structured_data_fields=body.structured_data_fields,
        states=body.states,
        starting_state=body.starting_state,
        welcome_message=body.welcome_message,
        agent_name=body.agent_name,
        live_transcript_enabled=body.live_transcript_enabled,
        model=body.model,
        model_temperature=body.model_temperature,
        voice_model=body.voice_model,
        voice_temperature=body.voice_temperature,
        stt_mode=body.stt_mode,
        denoising_mode=body.denoising_mode,
        ambient_sound=body.ambient_sound,
        ambient_sound_volume=body.ambient_sound_volume,
        backchannel_frequency=body.backchannel_frequency,
        backchannel_words=body.backchannel_words,
        responsiveness=body.responsiveness,
        reminder_trigger_ms=body.reminder_trigger_ms,
        reminder_max_count=body.reminder_max_count,
        end_call_after_silence_ms=body.end_call_after_silence_ms,
        max_call_duration_ms=body.max_call_duration_ms,
        begin_message_delay_ms=body.begin_message_delay_ms,
        allow_user_dtmf=body.allow_user_dtmf,
        allow_dtmf_interruption=body.allow_dtmf_interruption,
        data_storage_setting=body.data_storage_setting,
        pii_config=body.pii_config,
        post_call_analysis_model=body.post_call_analysis_model,
        handbook_config=body.handbook_config,
        status=status_value,
        vendor=retell_adapter.VENDOR_NAME,
        vendor_ref=vendor_ref,
        llm_ref=llm_ref,
    )

    if vendor_error is not None:
        # Re-raise the original upstream_failed error so the caller sees the
        # real 502/upstream_failed contract — the persisted "failed" record
        # above is a side effect for later retry/repair, not a success
        # response.
        raise vendor_error

    return _to_public(agent)


@router.patch(
    "/{agent_id}",
    response_model=AgentPublic,
    status_code=status.HTTP_200_OK,
    summary="Partially update an existing voice agent",
    description="""
Change one or more fields on an existing agent — the prompt, voice, tuning
fields, transfer configuration, custom tools, structured data fields, Multi
Prompt states, the fixed opening greeting (welcome_message), or your own
internal reference label (agent_name) — without recreating the agent from
scratch.

**This is a true partial update**: omit any field you don't want to change
and it keeps its current value. There is no need to re-send the whole
agent. A few exceptions where "which fields you included" itself matters:

- `custom_tools`, `structured_data_fields`, and `states` are whole-array-
  replace when included — send the complete list you want stored, not just
  the entries you're adding/removing.
- `transfer_number` follows null-means-omitted, same as every other field
  here — sending `transfer_number: null` alone does NOT clear an existing
  transfer number (it's indistinguishable from not mentioning the field at
  all once Pydantic parses the request). Use `clear_transfer_number: true`
  to explicitly remove it.
- `welcome_message` follows the identical null-means-omitted pattern — use
  `clear_welcome_message: true` to explicitly reset it back to the default
  improvised-greeting behavior. Note `welcome_message: ""` (empty string) is
  a real, DIFFERENT value from omitting the field or clearing it — it makes
  the agent wait silently for the caller to speak first, rather than
  improvising an opening line.
- `agent_name` follows the identical null-means-omitted pattern — use
  `clear_agent_name: true` to explicitly reset it back to unset.
- `starting_state` must be sent in the SAME request as a non-empty `states`
  — the agent's previous starting_state is never silently reused, since it
  may not even name one of the new states. Send `states: []` (with no
  `starting_state`) to collapse a Multi Prompt agent back to Single Prompt.

**`response_engine` cannot be changed via this endpoint** — switching a
`builtin` agent to `custom` mode (or back) is a real architectural
operation (deleting or creating the underlying conversation-brain object)
that is out of scope for this first pass; create a new agent if you need a
different mode.

`transfer_number`/`custom_tools`/`states`/`welcome_message` remain valid only
on a `builtin` agent, same restriction as agent creation. `agent_name` is
the one exception — it works on a `custom` agent too, since it lives on the
agent object rather than the conversation-brain object those other fields
depend on.
""",
    responses={
        404: {
            "description": "No agent exists with this id for the calling platform.",
        },
        422: {
            "description": "Request validation failed (e.g. transfer_number/custom_tools set "
            "on a 'custom' agent, an unknown field like response_engine, or an entirely "
            "empty request that changes nothing).",
        },
        502: {
            "description": "The voice vendor could not be reached or rejected the update. "
            "Whichever half of the change (agent-object fields vs conversation-brain fields) "
            "the vendor actually accepted is still reflected in our stored record and in the "
            "error response's context — see this endpoint's module docstring for the full "
            "partial-failure contract. Nothing is silently rolled back or silently dropped.",
        },
    },
)
async def update_agent(
    body: UpdateAgentRequest,
    caller: CurrentPlatform,
    db: DbDep,
    settings: Annotated[Settings, Depends(get_settings)],
    agent_id: Annotated[str, Path(description="Our agent id, from POST /agents' response.")],
) -> AgentPublic:
    """Partially update an existing agent. See this module's docstring for
    the full vendor-call-sequencing/partial-failure design, and
    `UpdateAgentRequest`'s own docstring (app/models/agent.py) for the full
    partial-update-semantics/merge-necessity reasoning.

    Step by step:
    1. Tenancy-scoped lookup (404 if missing/not-ours, never 403).
    2. Reject an empty request (nothing to do) with 422 — an accidental
       no-op PATCH is far more likely to be a caller bug (e.g. serializing
       an object with every field genuinely None) than a deliberate
       "confirm nothing changed" request; this codebase has no GET
       /agents/{id} for that latter use case to make sense of anyway.
    3. Reject transfer_number/custom_tools on a 'custom'-mode agent — same
       422, same message shape as CreateAgentRequest's own validator,
       reused directly (`_reject_transfer_fields_under_update`) rather than
       re-implemented, since the underlying reason (no vendor-side
       general_tools mechanism under 'custom') is identical.
    4. SSRF-check any NEW custom_tools[].webhook_url (only meaningful when
       custom_tools is actually present in this request — an omitted
       custom_tools means the existing, already-checked-at-creation-time
       URLs are untouched).
    5. Compute the merged, INTENDED final state (existing stored values,
       overridden field-by-field by whatever this request actually set).
    6. Call update-agent (always, for a builtin OR custom agent — every
       agent-object field lives there) and, for a builtin agent only,
       update-retell-llm (rebuilding the FULL general_tools array from the
       merged state whenever transfer_number/custom_tools/clear_transfer_number
       are involved — see UpdateAgentRequest's docstring for why that
       reconstruction is unavoidable).
    7. Persist our own record to reflect exactly what actually reached the
       vendor (each half independently kept-or-changed based on whether its
       own vendor call succeeded), then either return 200 with the new
       state or re-raise the real upstream_failed/502 if anything failed.
    """
    agent = await agent_repo.get_by_id(db, agent_id, platform_id=caller.id)
    if agent is None:
        raise AppError(
            code="not_found",
            message="No agent exists with that id.",
            status_code=404,
            field="agent_id",
        )

    if not body.has_any_field_set():
        raise AppError(
            code=CODE_VALIDATION,
            message="This request doesn't change anything — set at least one field to update.",
            status_code=422,
        )

    _reject_transfer_fields_under_update(body, current_response_engine=agent.response_engine)

    if body.custom_tools is not None:
        for tool in body.custom_tools:
            await reject_if_internal_url(tool.webhook_url, field="custom_tools.webhook_url")

    if body.states is not None:
        for state in body.states:
            for tool in state.tools:
                await reject_if_internal_url(
                    tool.webhook_url, field="states.tools.webhook_url"
                )

    # ── Step 5: compute the merged, intended final state ──────────────
    new_prompt = body.prompt if body.prompt is not None else agent.prompt
    new_voice_id = body.voice_id if body.voice_id is not None else agent.voice_id
    new_languages = body.languages_list if body.languages_list is not None else agent.languages
    new_voice_speed = body.voice_speed if body.voice_speed is not None else agent.voice_speed
    new_interruption_sensitivity = (
        body.interruption_sensitivity
        if body.interruption_sensitivity is not None
        else agent.interruption_sensitivity
    )
    new_enable_backchannel = (
        body.enable_backchannel
        if body.enable_backchannel is not None
        else agent.enable_backchannel
    )
    new_pronunciation_dictionary = (
        body.pronunciation_dictionary
        if body.pronunciation_dictionary is not None
        else agent.pronunciation_dictionary
    )
    # transfer_number: explicit clear_transfer_number wins over "leave
    # alone", an explicit new value wins over both — see
    # UpdateAgentRequest.clear_transfer_number's Field description for the
    # full omitted-vs-null-vs-clear-flag reasoning.
    if body.transfer_number is not None:
        new_transfer_number = body.transfer_number
    elif body.clear_transfer_number:
        new_transfer_number = None
    else:
        new_transfer_number = agent.transfer_number
    new_custom_tools = body.custom_tools if body.custom_tools is not None else agent.custom_tools
    new_structured_data_fields = (
        body.structured_data_fields
        if body.structured_data_fields is not None
        else agent.structured_data_fields
    )
    # states/starting_state: UpdateAgentRequest's own _validate_states_routing
    # already guarantees the two are set together whenever either is present
    # (see that validator's docstring) — omitted body.states here always
    # means "leave the agent's existing Multi Prompt configuration
    # unchanged," never "the caller wants states cleared without saying so."
    new_states = body.states if body.states is not None else agent.states
    new_starting_state = (
        body.starting_state if body.states is not None else agent.starting_state
    )
    # welcome_message: explicit clear_welcome_message wins over "leave
    # alone", an explicit new value (including "") wins over both — same
    # precedence as transfer_number/clear_transfer_number above. `is not
    # None`, deliberately, not a truthy check: "" is a real, meaningful
    # welcome_message value (wait silently), not an omitted one — see
    # UpdateAgentRequest.welcome_message's Field description.
    if body.welcome_message is not None:
        new_welcome_message = body.welcome_message
    elif body.clear_welcome_message:
        new_welcome_message = None
    else:
        new_welcome_message = agent.welcome_message
    # agent_name: explicit clear_agent_name wins over "leave alone", an
    # explicit new value wins over both — same precedence as
    # transfer_number/welcome_message above. Unlike welcome_message, no
    # `is not None` subtlety is needed for "" here — the vendor's own docs
    # describe no special empty-string meaning for agent_name, so this is a
    # plain optional label, not a three-state field.
    if body.agent_name is not None:
        new_agent_name = body.agent_name
    elif body.clear_agent_name:
        new_agent_name = None
    else:
        new_agent_name = agent.agent_name
    # live_transcript_enabled: plain optional bool, no omitted-vs-null
    # ambiguity to resolve (unlike transfer_number/welcome_message/
    # agent_name) — see UpdateAgentRequest.live_transcript_enabled's Field
    # description for why no separate clear flag is needed.
    new_live_transcript_enabled = (
        body.live_transcript_enabled
        if body.live_transcript_enabled is not None
        else agent.live_transcript_enabled
    )
    # Fall back to the agent's own CURRENT stored value (never a hardcoded
    # default) — these three are now genuinely persisted fields (see
    # AgentInDB's docstring for why that became necessary), so "the caller
    # didn't mention this" correctly means "keep what's already there,"
    # exactly like every other tuning field above, not "reset to
    # CreateAgentRequest's factory default."
    new_transfer_ring_duration_ms = (
        body.transfer_ring_duration_ms
        if body.transfer_ring_duration_ms is not None
        else agent.transfer_ring_duration_ms
    )
    new_transfer_on_hold_music = (
        body.transfer_on_hold_music
        if body.transfer_on_hold_music is not None
        else agent.transfer_on_hold_music
    )
    new_transfer_show_original_caller_id = (
        body.transfer_show_original_caller_id
        if body.transfer_show_original_caller_id is not None
        else agent.transfer_show_original_caller_id
    )
    # The ~19-field tuning-knob batch — every one is a plain scalar/array/
    # object with no omitted-vs-null-vs-clear-flag wrinkle (see
    # app/models/agent.py's module docstring, "~19-field tuning-knob batch"
    # section, "Update semantics" paragraph, for why): `None`/omitted on
    # UpdateAgentRequest always means "leave the current stored value
    # alone," same "fall back to the agent's own CURRENT stored value"
    # pattern as transfer_ring_duration_ms/transfer_on_hold_music/
    # transfer_show_original_caller_id just above, for the identical reason
    # (these are genuinely persisted fields — see AgentInDB's docstring).
    new_model = body.model if body.model is not None else agent.model
    new_model_temperature = (
        body.model_temperature if body.model_temperature is not None else agent.model_temperature
    )
    new_voice_model = body.voice_model if body.voice_model is not None else agent.voice_model
    new_voice_temperature = (
        body.voice_temperature if body.voice_temperature is not None else agent.voice_temperature
    )
    new_stt_mode = body.stt_mode if body.stt_mode is not None else agent.stt_mode
    new_denoising_mode = (
        body.denoising_mode if body.denoising_mode is not None else agent.denoising_mode
    )
    new_ambient_sound = (
        body.ambient_sound if body.ambient_sound is not None else agent.ambient_sound
    )
    new_ambient_sound_volume = (
        body.ambient_sound_volume
        if body.ambient_sound_volume is not None
        else agent.ambient_sound_volume
    )
    new_backchannel_frequency = (
        body.backchannel_frequency
        if body.backchannel_frequency is not None
        else agent.backchannel_frequency
    )
    new_backchannel_words = (
        body.backchannel_words if body.backchannel_words is not None else agent.backchannel_words
    )
    new_responsiveness = (
        body.responsiveness if body.responsiveness is not None else agent.responsiveness
    )
    new_reminder_trigger_ms = (
        body.reminder_trigger_ms
        if body.reminder_trigger_ms is not None
        else agent.reminder_trigger_ms
    )
    new_reminder_max_count = (
        body.reminder_max_count if body.reminder_max_count is not None else agent.reminder_max_count
    )
    new_end_call_after_silence_ms = (
        body.end_call_after_silence_ms
        if body.end_call_after_silence_ms is not None
        else agent.end_call_after_silence_ms
    )
    new_max_call_duration_ms = (
        body.max_call_duration_ms
        if body.max_call_duration_ms is not None
        else agent.max_call_duration_ms
    )
    new_begin_message_delay_ms = (
        body.begin_message_delay_ms
        if body.begin_message_delay_ms is not None
        else agent.begin_message_delay_ms
    )
    new_allow_user_dtmf = (
        body.allow_user_dtmf if body.allow_user_dtmf is not None else agent.allow_user_dtmf
    )
    new_allow_dtmf_interruption = (
        body.allow_dtmf_interruption
        if body.allow_dtmf_interruption is not None
        else agent.allow_dtmf_interruption
    )
    new_data_storage_setting = (
        body.data_storage_setting
        if body.data_storage_setting is not None
        else agent.data_storage_setting
    )
    new_pii_config = body.pii_config if body.pii_config is not None else agent.pii_config
    new_post_call_analysis_model = (
        body.post_call_analysis_model
        if body.post_call_analysis_model is not None
        else agent.post_call_analysis_model
    )
    new_handbook_config = (
        body.handbook_config if body.handbook_config is not None else agent.handbook_config
    )

    # ── Step 6: vendor calls ───────────────────────────────────────────
    # Tracks, independently, whether each vendor-side object's update
    # actually succeeded — this is what step 7 uses to decide which half of
    # the merged state to persist. See this module's docstring for the full
    # partial-failure reasoning.
    agent_update_succeeded = False
    llm_update_succeeded = agent.response_engine != ResponseEngine.BUILTIN  # n/a for custom mode
    vendor_error: AppError | None = None

    agent_update_body: dict[str, Any] = {
        "voice_id": new_voice_id,
        "language": retell_agent_adapter._build_vendor_language(new_languages),
        "voice_speed": new_voice_speed,
        "interruption_sensitivity": new_interruption_sensitivity,
        "enable_backchannel": new_enable_backchannel,
        "pronunciation_dictionary": [
            entry.model_dump() for entry in new_pronunciation_dictionary
        ],
    }
    # post_call_analysis_data is only meaningfully "updated" if the caller
    # actually touched structured_data_fields — otherwise leave the key out
    # entirely so Retell's own partial-merge behavior keeps whatever is
    # already stored there untouched, same "only include an optional key
    # when it has a real value/real intent to change it" convention as
    # create_agent()'s webhook_url handling.
    if body.structured_data_fields is not None:
        agent_update_body["post_call_analysis_data"] = (
            retell_agent_adapter._build_post_call_analysis_data(new_structured_data_fields)
        )
    # agent_name — an agent-object field (see app/models/agent.py's module
    # docstring, "agent_name" section), so it belongs on THIS call
    # (update-agent), never on the update-retell-llm call below, regardless
    # of response_engine. Same "only include when this request actually
    # touched it" convention as post_call_analysis_data just above — a new
    # value OR an explicit clear both count as "touched"; confirmed via the
    # same live WebFetch that update-agent is a genuine field-level
    # partial-merge endpoint, so omitting the key here correctly leaves
    # Retell's existing agent_name untouched, and explicitly sending None
    # (Python) / null (JSON) genuinely clears it — no "omitted vs explicit
    # null" wrinkle the way begin_message has, since we always distinguish
    # the two cases ourselves before building this body.
    if body.agent_name is not None or body.clear_agent_name:
        agent_update_body["agent_name"] = new_agent_name
    # live_transcript_enabled — an agent-object field, same placement as
    # agent_name just above (see app/models/agent.py's module docstring,
    # "live_transcript_enabled" section) — only included when THIS request
    # actually touched it, same "only include when touched" convention as
    # post_call_analysis_data/agent_name above. Unlike CREATE (which can
    # simply omit webhook_events for the disabled case),
    # `build_webhook_events_for_update` ALWAYS returns a real, explicit
    # array here — omitting the key on an UPDATE would leave the vendor's
    # existing (possibly non-default) value untouched instead of resetting
    # it, and an empty array means "no events at all," not "defaults" — see
    # that helper's own docstring for the full reasoning this mirrors from
    # `begin_message`/`states`' own identical "omitted means don't touch on
    # UPDATE" trap.
    if body.live_transcript_enabled is not None:
        agent_update_body["webhook_events"] = retell_agent_adapter.build_webhook_events_for_update(
            live_transcript_enabled=new_live_transcript_enabled
        )
    # The rest of the ~19-field tuning-knob batch (voice_model through
    # handbook_config) — all agent-object fields (see app/models/agent.py's
    # module docstring, "~19-field tuning-knob batch" section), so they
    # belong on THIS call too, same placement as agent_name/
    # live_transcript_enabled above. Built via the SAME
    # `_build_agent_tuning_fields()` helper create_agent()/
    # create_retell_llm_agent() already use — confirmed via the same fresh
    # WebFetch that update-agent remains field-level partial-merge for
    # every one of these, including backchannel_words (array) and
    # pii_config (object with an array inside), so `.update()`-ing the
    # merged batch into `agent_update_body` here is enough; there is no
    # `general_tools`/`states`-style whole-array-reconstruction gotcha for
    # any field in this batch (each is independently, directly settable,
    # with no cross-field assembly step on our own side).
    if any(
        getattr(body, field_name) is not None
        for field_name in (
            "voice_model",
            "voice_temperature",
            "stt_mode",
            "denoising_mode",
            "ambient_sound",
            "ambient_sound_volume",
            "backchannel_frequency",
            "backchannel_words",
            "responsiveness",
            "reminder_trigger_ms",
            "reminder_max_count",
            "end_call_after_silence_ms",
            "max_call_duration_ms",
            "begin_message_delay_ms",
            "allow_user_dtmf",
            "allow_dtmf_interruption",
            "data_storage_setting",
            "pii_config",
            "post_call_analysis_model",
            "handbook_config",
        )
    ):
        agent_update_body.update(
            retell_agent_adapter._build_agent_tuning_fields(
                voice_model=new_voice_model,
                voice_temperature=new_voice_temperature,
                stt_mode=new_stt_mode,
                denoising_mode=new_denoising_mode,
                ambient_sound=new_ambient_sound,
                ambient_sound_volume=new_ambient_sound_volume,
                backchannel_frequency=new_backchannel_frequency,
                backchannel_words=new_backchannel_words,
                responsiveness=new_responsiveness,
                reminder_trigger_ms=new_reminder_trigger_ms,
                reminder_max_count=new_reminder_max_count,
                end_call_after_silence_ms=new_end_call_after_silence_ms,
                max_call_duration_ms=new_max_call_duration_ms,
                begin_message_delay_ms=new_begin_message_delay_ms,
                allow_user_dtmf=new_allow_user_dtmf,
                allow_dtmf_interruption=new_allow_dtmf_interruption,
                data_storage_setting=new_data_storage_setting,
                pii_config=new_pii_config,
                post_call_analysis_model=new_post_call_analysis_model,
                handbook_config=new_handbook_config,
            )
        )

    if agent.vendor_ref is not None:
        try:
            await retell_agent_adapter.update_agent(
                settings, agent_id=agent.vendor_ref, body=agent_update_body
            )
            agent_update_succeeded = True
        except AppError as exc:
            vendor_error = exc
    else:
        # No real vendor-side agent to update (this agent's own creation
        # never succeeded — status='failed', see POST /agents' persist-on-
        # vendor-failure precedent). Nothing to call; treat as "succeeded"
        # for the purposes of step 7 so the merged non-vendor-backed fields
        # still get saved on our own side even though there is no vendor
        # object to reconcile against.
        agent_update_succeeded = True

    llm_touch_needed = (
        body.transfer_number is not None
        or body.clear_transfer_number
        or body.custom_tools is not None
        or body.prompt is not None
        or body.transfer_ring_duration_ms is not None
        or body.transfer_on_hold_music is not None
        or body.transfer_show_original_caller_id is not None
        or body.states is not None
        or body.welcome_message is not None
        or body.clear_welcome_message
        # model/model_temperature are the two LLM-object fields of the
        # ~19-field tuning-knob batch (see app/models/agent.py's module
        # docstring) — everything else in that batch is agent-object (see
        # the agent_update_body block above), so only these two belong here.
        or body.model is not None
        or body.model_temperature is not None
    )
    if (
        agent.response_engine == ResponseEngine.BUILTIN
        and agent.llm_ref is not None
        and llm_touch_needed
    ):
        llm_update_body: dict[str, Any] = {
            "general_prompt": new_prompt,
            "general_tools": retell_agent_adapter._build_general_tools(
                transfer_number=new_transfer_number,
                transfer_ring_duration_ms=new_transfer_ring_duration_ms,
                transfer_on_hold_music=new_transfer_on_hold_music,
                transfer_show_original_caller_id=new_transfer_show_original_caller_id,
                custom_tools=new_custom_tools,
                custom_tool_api_endpoint=f"{settings.BASE_URL}/webhooks/retell/custom-tool",
            ),
            # model_temperature — ALWAYS included, unconditionally, whenever
            # the LLM object is touched at all, same "always rebuilt
            # alongside general_prompt/general_tools" treatment those two
            # already get (this codebase's own pre-existing convention —
            # see create_retell_llm()'s own docstring for why it's sent
            # explicitly even at its default value, for a self-documenting
            # request body).
            "model_temperature": new_model_temperature,
        }
        # model — the sibling LLM-object field of the ~19-field tuning-knob
        # batch — only included when THIS request actually touched it, same
        # "only include when touched" convention as agent_name on
        # agent_update_body above: `None`-means-omit-the-key here (the
        # vendor's own bare default applies), not a real value to always
        # resend, unlike model_temperature just above.
        if body.model is not None:
            llm_update_body["model"] = new_model
        # states is only included when THIS request actually touched it —
        # unlike general_tools above (always rebuilt/sent whenever
        # llm_touch_needed is true, since prompt/transfer/custom_tools all
        # feed into it), states has no such shared coupling to any other
        # field here. Sent EXPLICITLY as states=[] when body.states was set
        # to an empty array (collapsing Multi Prompt back to Single Prompt)
        # — see retell_agent_adapter.update_retell_llm's own docstring for
        # why omitting the key entirely would instead leave the vendor's
        # existing states array untouched, the opposite of what a clear
        # needs.
        if body.states is not None:
            custom_tool_api_endpoint = f"{settings.BASE_URL}/webhooks/retell/custom-tool"
            llm_update_body["states"] = retell_agent_adapter._build_states(
                new_states, custom_tool_api_endpoint=custom_tool_api_endpoint
            )
            llm_update_body["starting_state"] = new_starting_state
        # begin_message (welcome_message's real vendor field name) —
        # ALWAYS included, explicitly, whenever the LLM object is touched at
        # all (never conditionally omitted the way states/starting_state
        # are) — confirmed live against a real Retell account this session
        # that this is required, not optional, and got it wrong on a first
        # pass: update-retell-llm is a genuine field-level partial-merge
        # endpoint, so simply OMITTING begin_message from this body when
        # new_welcome_message is None does NOT clear a previously-set
        # begin_message on the vendor's side — it leaves Retell's existing
        # value untouched, exactly the same "omitted means don't touch"
        # trap `states` already had to work around (see that field's own
        # comment just above) applied to a scalar field instead of an
        # array. The confirmed-live fix: send `begin_message: null`
        # EXPLICITLY (Python None, not an omitted key) to genuinely clear
        # it back to Retell's own "unset -> improvise" behavior — verified
        # against a real llm_id this session: PATCHing {"begin_message":
        # null} removes the key entirely from the vendor's own stored
        # object on the next GET, exactly the improvised-greeting default.
        # `is not None` on new_welcome_message decides WHICH explicit value
        # to send (the real string/"" vs None-to-clear) — never whether to
        # send the key at all.
        llm_update_body["begin_message"] = new_welcome_message
        try:
            await retell_agent_adapter.update_retell_llm(
                settings, llm_id=agent.llm_ref, body=llm_update_body
            )
            llm_update_succeeded = True
        except AppError as exc:
            llm_update_succeeded = False
            if vendor_error is None:
                vendor_error = exc
    elif agent.response_engine == ResponseEngine.BUILTIN and agent.llm_ref is None:
        # Same "never had a real vendor object to begin with" case as the
        # vendor_ref branch above, mirrored for the LLM side.
        llm_update_succeeded = True

    # ── Step 7: persist exactly what actually reached the vendor ──────
    # Each half of the merged state is only actually stored if ITS OWN
    # vendor call succeeded (or wasn't needed at all) — a failed half falls
    # back to the agent's PREVIOUS stored value for exactly those fields,
    # never the caller's requested-but-not-yet-real value. See this
    # module's docstring for the full reasoning.
    persisted_prompt = new_prompt if llm_update_succeeded else agent.prompt
    persisted_transfer_number = (
        new_transfer_number if llm_update_succeeded else agent.transfer_number
    )
    persisted_custom_tools = new_custom_tools if llm_update_succeeded else agent.custom_tools
    persisted_transfer_ring_duration_ms = (
        new_transfer_ring_duration_ms if llm_update_succeeded else agent.transfer_ring_duration_ms
    )
    persisted_transfer_on_hold_music = (
        new_transfer_on_hold_music if llm_update_succeeded else agent.transfer_on_hold_music
    )
    persisted_transfer_show_original_caller_id = (
        new_transfer_show_original_caller_id
        if llm_update_succeeded
        else agent.transfer_show_original_caller_id
    )
    # states/starting_state live on the SAME vendor object (the LLM, not
    # the agent) as prompt/transfer_number/custom_tools above — so they
    # follow llm_update_succeeded, not agent_update_succeeded, same
    # reasoning as every other LLM-object field in this block.
    persisted_states = new_states if llm_update_succeeded else agent.states
    persisted_starting_state = (
        new_starting_state if llm_update_succeeded else agent.starting_state
    )
    # welcome_message lives on the same vendor object (the LLM) as
    # prompt/transfer_number/states above — same llm_update_succeeded
    # gating, same reasoning.
    persisted_welcome_message = (
        new_welcome_message if llm_update_succeeded else agent.welcome_message
    )
    # model/model_temperature — the two LLM-object fields of the ~19-field
    # tuning-knob batch — live on the SAME vendor object (the LLM) as
    # prompt/states/welcome_message above, so they follow
    # llm_update_succeeded too, same reasoning.
    persisted_model = new_model if llm_update_succeeded else agent.model
    persisted_model_temperature = (
        new_model_temperature if llm_update_succeeded else agent.model_temperature
    )

    persisted_voice_id = new_voice_id if agent_update_succeeded else agent.voice_id
    persisted_languages = new_languages if agent_update_succeeded else agent.languages
    persisted_voice_speed = new_voice_speed if agent_update_succeeded else agent.voice_speed
    persisted_interruption_sensitivity = (
        new_interruption_sensitivity
        if agent_update_succeeded
        else agent.interruption_sensitivity
    )
    persisted_enable_backchannel = (
        new_enable_backchannel if agent_update_succeeded else agent.enable_backchannel
    )
    persisted_pronunciation_dictionary = (
        new_pronunciation_dictionary
        if agent_update_succeeded
        else agent.pronunciation_dictionary
    )
    persisted_structured_data_fields = (
        new_structured_data_fields if agent_update_succeeded else agent.structured_data_fields
    )
    # agent_name lives on the SAME vendor object (the agent, not the LLM) as
    # voice_id/structured_data_fields above — so it follows
    # agent_update_succeeded, not llm_update_succeeded, same reasoning as
    # every other agent-object field in this block.
    persisted_agent_name = new_agent_name if agent_update_succeeded else agent.agent_name
    # live_transcript_enabled lives on the SAME vendor object (the agent,
    # not the LLM) as voice_id/agent_name above — so it follows
    # agent_update_succeeded, not llm_update_succeeded, same reasoning as
    # every other agent-object field in this block.
    persisted_live_transcript_enabled = (
        new_live_transcript_enabled if agent_update_succeeded else agent.live_transcript_enabled
    )
    # The rest of the ~19-field tuning-knob batch (voice_model through
    # handbook_config) — all agent-object fields, same agent_update_succeeded
    # gating, same reasoning as voice_id/agent_name/live_transcript_enabled
    # above.
    persisted_voice_model = new_voice_model if agent_update_succeeded else agent.voice_model
    persisted_voice_temperature = (
        new_voice_temperature if agent_update_succeeded else agent.voice_temperature
    )
    persisted_stt_mode = new_stt_mode if agent_update_succeeded else agent.stt_mode
    persisted_denoising_mode = (
        new_denoising_mode if agent_update_succeeded else agent.denoising_mode
    )
    persisted_ambient_sound = (
        new_ambient_sound if agent_update_succeeded else agent.ambient_sound
    )
    persisted_ambient_sound_volume = (
        new_ambient_sound_volume if agent_update_succeeded else agent.ambient_sound_volume
    )
    persisted_backchannel_frequency = (
        new_backchannel_frequency if agent_update_succeeded else agent.backchannel_frequency
    )
    persisted_backchannel_words = (
        new_backchannel_words if agent_update_succeeded else agent.backchannel_words
    )
    persisted_responsiveness = (
        new_responsiveness if agent_update_succeeded else agent.responsiveness
    )
    persisted_reminder_trigger_ms = (
        new_reminder_trigger_ms if agent_update_succeeded else agent.reminder_trigger_ms
    )
    persisted_reminder_max_count = (
        new_reminder_max_count if agent_update_succeeded else agent.reminder_max_count
    )
    persisted_end_call_after_silence_ms = (
        new_end_call_after_silence_ms
        if agent_update_succeeded
        else agent.end_call_after_silence_ms
    )
    persisted_max_call_duration_ms = (
        new_max_call_duration_ms if agent_update_succeeded else agent.max_call_duration_ms
    )
    persisted_begin_message_delay_ms = (
        new_begin_message_delay_ms if agent_update_succeeded else agent.begin_message_delay_ms
    )
    persisted_allow_user_dtmf = (
        new_allow_user_dtmf if agent_update_succeeded else agent.allow_user_dtmf
    )
    persisted_allow_dtmf_interruption = (
        new_allow_dtmf_interruption if agent_update_succeeded else agent.allow_dtmf_interruption
    )
    persisted_data_storage_setting = (
        new_data_storage_setting if agent_update_succeeded else agent.data_storage_setting
    )
    persisted_pii_config = new_pii_config if agent_update_succeeded else agent.pii_config
    persisted_post_call_analysis_model = (
        new_post_call_analysis_model
        if agent_update_succeeded
        else agent.post_call_analysis_model
    )
    persisted_handbook_config = (
        new_handbook_config if agent_update_succeeded else agent.handbook_config
    )

    await agent_repo.update(
        db,
        agent_id,
        platform_id=caller.id,
        prompt=persisted_prompt,
        voice_id=persisted_voice_id,
        languages=persisted_languages,
        voice_speed=persisted_voice_speed,
        interruption_sensitivity=persisted_interruption_sensitivity,
        enable_backchannel=persisted_enable_backchannel,
        pronunciation_dictionary=persisted_pronunciation_dictionary,
        transfer_number=persisted_transfer_number,
        transfer_ring_duration_ms=persisted_transfer_ring_duration_ms,
        transfer_on_hold_music=persisted_transfer_on_hold_music,
        transfer_show_original_caller_id=persisted_transfer_show_original_caller_id,
        custom_tools=persisted_custom_tools,
        structured_data_fields=persisted_structured_data_fields,
        states=persisted_states,
        starting_state=persisted_starting_state,
        welcome_message=persisted_welcome_message,
        agent_name=persisted_agent_name,
        live_transcript_enabled=persisted_live_transcript_enabled,
        model=persisted_model,
        model_temperature=persisted_model_temperature,
        voice_model=persisted_voice_model,
        voice_temperature=persisted_voice_temperature,
        stt_mode=persisted_stt_mode,
        denoising_mode=persisted_denoising_mode,
        ambient_sound=persisted_ambient_sound,
        ambient_sound_volume=persisted_ambient_sound_volume,
        backchannel_frequency=persisted_backchannel_frequency,
        backchannel_words=persisted_backchannel_words,
        responsiveness=persisted_responsiveness,
        reminder_trigger_ms=persisted_reminder_trigger_ms,
        reminder_max_count=persisted_reminder_max_count,
        end_call_after_silence_ms=persisted_end_call_after_silence_ms,
        max_call_duration_ms=persisted_max_call_duration_ms,
        begin_message_delay_ms=persisted_begin_message_delay_ms,
        allow_user_dtmf=persisted_allow_user_dtmf,
        allow_dtmf_interruption=persisted_allow_dtmf_interruption,
        data_storage_setting=persisted_data_storage_setting,
        pii_config=persisted_pii_config,
        post_call_analysis_model=persisted_post_call_analysis_model,
        handbook_config=persisted_handbook_config,
    )

    updated_agent = await agent_repo.get_by_id(db, agent_id, platform_id=caller.id)
    assert updated_agent is not None  # just wrote it moments ago, same tenancy scope

    if vendor_error is not None:
        raise vendor_error

    return _to_public(updated_agent)


@router.post(
    "/{agent_id}/numbers",
    response_model=PhoneNumberPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Buy a new phone number through us and bind it to this agent for inbound calls",
    responses={
        404: {
            "description": "No agent exists with this id for the calling platform.",
        },
        422: {
            "description": "The agent exists but never successfully finished creation on the "
            "voice vendor (status 'failed') — a number can't be attached to it yet.",
        },
        502: {
            "description": "The voice vendor could not be reached or rejected the phone "
            "number purchase request (e.g. requested area code/number unavailable).",
        },
    },
)
async def create_phone_number(
    body: CreatePhoneNumberRequest,
    caller: CurrentPlatform,
    db: DbDep,
    settings: Annotated[Settings, Depends(get_settings)],
    agent_id: Annotated[str, Path(description="Our agent id, from POST /agents' response.")],
) -> PhoneNumberPublic:
    """Buy a new phone number through us (Phase 1 item 4, Option 1 of
    "Telephony: two options" — buy-new; BYO SIP is separate future work) and
    bind it to `agent_id` for inbound calls, in the same vendor call.

    Every body field is optional — the voice vendor picks sensible values
    (e.g. a random available US number) for anything omitted. Request a
    specific `phone_number` if Platform X needs a particular E.164 number;
    otherwise let us choose one, optionally narrowed by `area_code`/
    `toll_free`/`country_code`.

    The agent must belong to the calling platform (a cross-platform lookup
    404s, same as everywhere else, not 403 — see the standards doc's
    tenancy rules) and must have actually finished creation on the vendor
    side (`status != "failed"`) — an agent whose own vendor creation call
    failed has no real vendor-side agent id to bind a number to.
    """
    agent = await agent_repo.get_by_id(db, agent_id, platform_id=caller.id)
    if agent is None:
        raise AppError(
            code="not_found",
            message="No agent exists with that id.",
            status_code=404,
            field="agent_id",
        )
    if agent.status == AgentStatus.FAILED or agent.vendor_ref is None:
        # Persist-on-vendor-failure (see POST /agents) means a "failed" agent
        # record can exist with no real counterpart on Retell's side — there
        # is no Retell agent_id to put in inbound_agents, so this must be
        # rejected up front rather than attempting (and failing) the vendor
        # call anyway.
        raise AppError(
            code=CODE_VALIDATION,
            message="This agent never finished creation on the voice vendor, so a phone "
            "number can't be attached to it yet. Retry creating the agent first.",
            status_code=422,
            field="agent_id",
        )

    result = await retell_adapter.create_phone_number(
        settings,
        retell_agent_id=agent.vendor_ref,
        area_code=body.area_code,
        toll_free=body.toll_free,
        country_code=body.country_code,
        phone_number=body.phone_number,
        nickname=body.nickname,
        # Without this, the voice vendor has nowhere to send the inbound-call
        # webhook and GET/POST /webhooks/retell/inbound never fires for a
        # call to this number — see app/routers/webhooks.py.
        inbound_webhook_url=f"{settings.BASE_URL}/webhooks/retell/inbound",
    )

    number = await phone_number_repo.create(
        db,
        platform_id=caller.id,
        agent_id=agent.id,
        phone_number=result.phone_number,
        area_code=result.area_code,
        nickname=result.nickname,
        vendor=retell_adapter.VENDOR_NAME,
    )
    return _to_public_number(number)


@router.post(
    "/{agent_id}/numbers/byo",
    response_model=PhoneNumberPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Import a phone number from your own SIP trunk and bind it to this agent",
    description="""
Bring your own SIP trunk (Option 2 of telephony, as opposed to `POST
/agents/{agent_id}/numbers` which buys a new number through us — Option 1).
Use this when Platform X already owns a phone number and a SIP trunk at a
third-party telephony provider and wants calls routed through it instead of
buying a new number.

**Prerequisites you must complete yourself before calling this endpoint**
(these happen on your own SIP provider's dashboard — we cannot automate
them):
1. Configure a SIP trunk for this number at your own telephony provider
   (Twilio, Telnyx, Vonage, and others each have a dedicated setup guide for
   connecting to a voice platform like ours).
2. Whitelist our IP ranges on your SIP provider's side, or inbound/outbound
   calls will not reach us:
   `18.98.16.120/30`, `3.42.144.0/23`, `153.57.128.0/18`,
   `143.223.88.0/21`, `161.115.160.0/19`.

`phone_number` and `termination_uri` are required; every other field is
optional. Agent binding happens in this same call via `inbound_agents`,
same mechanism as the buy-new endpoint.

If your trunk requires authentication, `sip_trunk_auth_username`/
`sip_trunk_auth_password` are forwarded to the voice vendor but are **never
stored on our side and never logged** — see ImportPhoneNumberRequest's
docstring for the full reasoning. Nothing in this endpoint's response ever
echoes these values back either.
""",
    responses={
        404: {
            "description": "No agent exists with this id for the calling platform.",
        },
        422: {
            "description": "The agent exists but never successfully finished creation on the "
            "voice vendor (status 'failed') — a number can't be attached to it yet.",
        },
        502: {
            "description": "The voice vendor could not be reached, or rejected the import "
            "(e.g. an unreachable/misconfigured SIP trunk, or a phone_number that fails "
            "validation). Confirm your trunk is reachable and our IP ranges are "
            "whitelisted on your provider's side.",
        },
    },
)
async def import_phone_number(
    body: ImportPhoneNumberRequest,
    caller: CurrentPlatform,
    db: DbDep,
    settings: Annotated[Settings, Depends(get_settings)],
    agent_id: Annotated[str, Path(description="Our agent id, from POST /agents' response.")],
) -> PhoneNumberPublic:
    """Import a phone number from Platform X's own SIP trunk (Phase 1 item 4,
    Option 2 of "Telephony: two options" — bring your own SIP trunk) and
    bind it to `agent_id` for inbound calls, in the same vendor call.

    Same tenancy/status guard as POST /agents/{agent_id}/numbers: the agent
    must belong to the calling platform (cross-platform lookup 404s, never
    403) and must have actually finished creation on the vendor side
    (`status != "failed"`) — there's no real Retell agent_id to bind to
    otherwise.

    Same no-persist-on-vendor-failure pattern as the buy-new sibling: if
    Retell rejects the import, no `PhoneNumbers` document is written. A
    failed import isn't a real resource worth a placeholder record for; the
    caller just retries.
    """
    agent = await agent_repo.get_by_id(db, agent_id, platform_id=caller.id)
    if agent is None:
        raise AppError(
            code="not_found",
            message="No agent exists with that id.",
            status_code=404,
            field="agent_id",
        )
    if agent.status == AgentStatus.FAILED or agent.vendor_ref is None:
        raise AppError(
            code=CODE_VALIDATION,
            message="This agent never finished creation on the voice vendor, so a phone "
            "number can't be attached to it yet. Retry creating the agent first.",
            status_code=422,
            field="agent_id",
        )

    result = await retell_adapter.import_phone_number(
        settings,
        phone_number=body.phone_number,
        termination_uri=body.termination_uri,
        retell_agent_id=agent.vendor_ref,
        sip_trunk_auth_username=body.sip_trunk_auth_username,
        sip_trunk_auth_password=body.sip_trunk_auth_password,
        ignore_e164_validation=body.ignore_e164_validation,
        transport=body.transport,
        nickname=body.nickname,
        inbound_webhook_url=f"{settings.BASE_URL}/webhooks/retell/inbound",
    )

    # sip_trunk_auth_username/sip_trunk_auth_password are deliberately never
    # passed to phone_number_repo.create() below — see
    # ImportPhoneNumberRequest's docstring for why they are never persisted.
    number = await phone_number_repo.create(
        db,
        platform_id=caller.id,
        agent_id=agent.id,
        phone_number=result.phone_number,
        area_code=result.area_code,
        nickname=result.nickname,
        vendor=retell_adapter.VENDOR_NAME,
    )
    return _to_public_number(number)
