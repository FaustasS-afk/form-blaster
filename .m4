Pick the CONTACT form, not the subscribe form

THE BUG THE USER CAUGHT WHILE WATCHING
  jewelryboxestatebuyers.com/contact/ has TWO Gravity Forms:
    gform_1 = First Name + Email only      <- a subscribe/stub
    gform_2 = First+Last+Email+Phone+textarea <- the REAL contact form
  The bot filled gform_1 and moved on. The pitch textarea does not exist there,
  so the whole message was silently dropped and the "send" still counted.

TWO STACKED CAUSES
  1. _extract_inputs() used a flat
       document.querySelectorAll('input,textarea,select')
     across the WHOLE page, so it returned fields from EVERY form at once. It
     mapped the first name/email it saw and never reached the real form's
     message box.
  2. _fill_and_submit() clicked page.locator(sel).last -- the last matching
     button anywhere -- so on a two-form page it hit the wrong form's submit.

FIX
  _extract_inputs() now enumerates <form> elements, scores each, and returns
  ONLY the winning form's fields:
      +6 textarea (message box -- carries the pitch, subscribe boxes lack one)
      +3 email field
      +2 phone field
      +1 per fillable field (cap 6)
      +2 own submit control
      -5 subscribe/newsletter/signup/footer wording in id|class|action
      -4 the same wording in field names/placeholders/labels
  The winning form id is stashed in cfg so the submit click can be scoped to
  it (#gform_2 first, then page-wide fallback), and .first is used within a
  scope instead of .last.

VERIFIED on the exact URL flagged:
  forms on page 2 | chosen gform_2 | score 18 | 5 fields incl. the textarea
  MESSAGE BOX INCLUDED: True
