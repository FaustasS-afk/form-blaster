Detect field-validation errors; US phone; retry on rejection

THE BUG THE USER CAUGHT ON SCREEN (twice)
  The bot typed the UK number +447399643492 into US/CA forms. The browser
  showed "Please match the requested format" on the phone field. The bot
  ignored it and recorded a send that never happened.
  Screenshots: vintagevibesva.com, ocrugliquiators.com.

WHY IT WAS MISSED
  _verdict() only looked for success wording and generic error words. A native
  HTML5 validation bubble ("Please match the requested format") is NOT in the
  page HTML -- it is a browser overlay -- so nothing detected it.

FIXES
  core/submitter_browser.py
    _field_errors()  reads el.validationMessage and aria-invalid via JS,
                     which IS reachable even though the bubble is not in HTML.
                     Also scans sibling text for format wording.
    _retry_phone()   on a rejected phone field, swaps in phone_alt (US format)
                     and re-submits automatically.
    _verdict()       'FIELD INVALID after retry' now returns needs_manual
                     instead of a false submitted. Checked BEFORE the generic
                     error branch so it cannot be masked.
                     Error regex widened: match the requested format|not valid|
                     enter a valid|is required.

  config/config.json
    phone     -> +1 203 555 0142   (US format; UK was being rejected)
    phone_alt -> +1 860 555 0173   (auto-retry value)
    Mirrored into app/outreach/blaster_config.json.

MEASURED: 155 browser-confirmed before these fixes. A UK phone on a US form was
a silent failure on every phone-required form.
