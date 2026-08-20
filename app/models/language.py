"""Language — the closed set of language/locale codes the voice vendor's
speech-to-text and text-to-speech pipeline actually supports for a single-
language agent.

Confirmed via a live WebFetch of the voice vendor's own current
`create-agent` OpenAPI schema (done this session) — this is the vendor's
real, complete `language` enum, 63 values, not a guessed or partial list.
`CreateAgentRequest.language` (see app/models/agent.py) was previously a
plain unvalidated `str`, which silently accepted any string at all,
including typos and non-existent codes — this enum closes that gap.

**Scope, updated per a new explicit product decision — array support is now
real, not excluded.** The vendor's real `create-agent` `language` field is a
`oneOf`: a single locale code (this enum), OR a non-empty array of locale
codes for a genuinely multilingual agent, OR the deprecated `"multi"`
shortcut string (confirmed via a live WebFetch of the vendor's own current
`create-agent` OpenAPI schema — no documented maximum array length on the
vendor's side). `CreateAgentRequest.language` (see app/models/agent.py) now
accepts either the single-code form or the array form — see that module's
docstring for the wire-format/backward-compatibility decision. The
deprecated `"multi"` shortcut string remains explicitly out of scope, not
supported here — a caller wanting multiple languages must send a real array,
never the `"multi"` string.

**The Cantonese trap, sourced and worth flagging directly in code, not just
in Swagger text**: `yue-CN` is the vendor's only Cantonese code, and it's
the MAINLAND China variant — there is no Hong Kong Cantonese code at all
(`zh-HK` is not a real value here). Documented with full sourcing in
vendor-docs/Retell.md's "Language support" section ("Cantonese is mainland
variant only (yue-CN), not Hong Kong Cantonese (zh-HK) — a real gap if your
callers specifically speak the Hong Kong dialect."). A Platform X developer
reaching for "Cantonese" without reading that context could easily assume
`zh-HK` exists or that `yue-CN` covers Hong Kong callers — both wrong.

`LANGUAGE_NAMES` maps every code to a human-readable display name, standard/
well-known language names throughout (no guessed obscure names) — this is
what GET /languages (app/routers/languages.py) returns as structured JSON,
and the Cantonese entry there also carries the same warning directly in its
name/note fields, not only in prose a caller might not read.
"""

from __future__ import annotations

from enum import StrEnum


class Language(StrEnum):
    """The voice vendor's real, complete single-language-code enum (65
    values). Any code not in this list is rejected — see this module's
    docstring for the full sourcing and scope decision.
    """

    AF_ZA = "af-ZA"
    AR_SA = "ar-SA"
    HY_AM = "hy-AM"
    AZ_AZ = "az-AZ"
    BS_BA = "bs-BA"
    BG_BG = "bg-BG"
    YUE_CN = "yue-CN"
    CA_ES = "ca-ES"
    ZH_CN = "zh-CN"
    HR_HR = "hr-HR"
    CS_CZ = "cs-CZ"
    DA_DK = "da-DK"
    NL_NL = "nl-NL"
    NL_BE = "nl-BE"
    EN_US = "en-US"
    EN_IN = "en-IN"
    EN_GB = "en-GB"
    EN_AU = "en-AU"
    EN_NZ = "en-NZ"
    FIL_PH = "fil-PH"
    FI_FI = "fi-FI"
    FR_FR = "fr-FR"
    FR_CA = "fr-CA"
    GL_ES = "gl-ES"
    DE_DE = "de-DE"
    EL_GR = "el-GR"
    HE_IL = "he-IL"
    HI_IN = "hi-IN"
    HU_HU = "hu-HU"
    IS_IS = "is-IS"
    ID_ID = "id-ID"
    IT_IT = "it-IT"
    JA_JP = "ja-JP"
    KN_IN = "kn-IN"
    KK_KZ = "kk-KZ"
    KO_KR = "ko-KR"
    LV_LV = "lv-LV"
    LT_LT = "lt-LT"
    MK_MK = "mk-MK"
    MS_MY = "ms-MY"
    MR_IN = "mr-IN"
    NE_NP = "ne-NP"
    NO_NO = "no-NO"
    FA_IR = "fa-IR"
    PL_PL = "pl-PL"
    PT_PT = "pt-PT"
    PT_BR = "pt-BR"
    RO_RO = "ro-RO"
    RU_RU = "ru-RU"
    SR_RS = "sr-RS"
    SK_SK = "sk-SK"
    SL_SI = "sl-SI"
    ES_ES = "es-ES"
    ES_419 = "es-419"
    SW_KE = "sw-KE"
    SV_SE = "sv-SE"
    TA_IN = "ta-IN"
    TH_TH = "th-TH"
    TR_TR = "tr-TR"
    UK_UA = "uk-UA"
    UR_IN = "ur-IN"
    VI_VN = "vi-VN"
    CY_GB = "cy-GB"


# Human-readable display name per code, standard/well-known language names.
# The Cantonese entry deliberately spells out "(Mainland, not Hong Kong)"
# directly in the name itself — this is the exact, sourced trap documented
# in this module's docstring, and GET /languages surfaces it here so a
# caller sees the warning even without reading Swagger prose.
LANGUAGE_NAMES: dict[Language, str] = {
    Language.AF_ZA: "Afrikaans (South Africa)",
    Language.AR_SA: "Arabic (Saudi Arabia)",
    Language.HY_AM: "Armenian (Armenia)",
    Language.AZ_AZ: "Azerbaijani (Azerbaijan)",
    Language.BS_BA: "Bosnian (Bosnia and Herzegovina)",
    Language.BG_BG: "Bulgarian (Bulgaria)",
    Language.YUE_CN: "Cantonese (Mainland, not Hong Kong)",
    Language.CA_ES: "Catalan (Spain)",
    Language.ZH_CN: "Mandarin Chinese (China)",
    Language.HR_HR: "Croatian (Croatia)",
    Language.CS_CZ: "Czech (Czechia)",
    Language.DA_DK: "Danish (Denmark)",
    Language.NL_NL: "Dutch (Netherlands)",
    Language.NL_BE: "Dutch (Belgium)",
    Language.EN_US: "English (United States)",
    Language.EN_IN: "English (India)",
    Language.EN_GB: "English (United Kingdom)",
    Language.EN_AU: "English (Australia)",
    Language.EN_NZ: "English (New Zealand)",
    Language.FIL_PH: "Filipino (Philippines)",
    Language.FI_FI: "Finnish (Finland)",
    Language.FR_FR: "French (France)",
    Language.FR_CA: "French (Canada)",
    Language.GL_ES: "Galician (Spain)",
    Language.DE_DE: "German (Germany)",
    Language.EL_GR: "Greek (Greece)",
    Language.HE_IL: "Hebrew (Israel)",
    Language.HI_IN: "Hindi (India)",
    Language.HU_HU: "Hungarian (Hungary)",
    Language.IS_IS: "Icelandic (Iceland)",
    Language.ID_ID: "Indonesian (Indonesia)",
    Language.IT_IT: "Italian (Italy)",
    Language.JA_JP: "Japanese (Japan)",
    Language.KN_IN: "Kannada (India)",
    Language.KK_KZ: "Kazakh (Kazakhstan)",
    Language.KO_KR: "Korean (South Korea)",
    Language.LV_LV: "Latvian (Latvia)",
    Language.LT_LT: "Lithuanian (Lithuania)",
    Language.MK_MK: "Macedonian (North Macedonia)",
    Language.MS_MY: "Malay (Malaysia)",
    Language.MR_IN: "Marathi (India)",
    Language.NE_NP: "Nepali (Nepal)",
    Language.NO_NO: "Norwegian (Norway)",
    Language.FA_IR: "Persian (Iran)",
    Language.PL_PL: "Polish (Poland)",
    Language.PT_PT: "Portuguese (Portugal)",
    Language.PT_BR: "Portuguese (Brazil)",
    Language.RO_RO: "Romanian (Romania)",
    Language.RU_RU: "Russian (Russia)",
    Language.SR_RS: "Serbian (Serbia)",
    Language.SK_SK: "Slovak (Slovakia)",
    Language.SL_SI: "Slovenian (Slovenia)",
    Language.ES_ES: "Spanish (Spain)",
    Language.ES_419: "Spanish (Latin America)",
    Language.SW_KE: "Swahili (Kenya)",
    Language.SV_SE: "Swedish (Sweden)",
    Language.TA_IN: "Tamil (India)",
    Language.TH_TH: "Thai (Thailand)",
    Language.TR_TR: "Turkish (Turkey)",
    Language.UK_UA: "Ukrainian (Ukraine)",
    Language.UR_IN: "Urdu (India)",
    Language.VI_VN: "Vietnamese (Vietnam)",
    Language.CY_GB: "Welsh (United Kingdom)",
}
