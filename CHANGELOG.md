# Changelog

All notable changes of `account_bill_capture`. Versions follow the Odoo scheme `19.0.x.y.z`.

## 19.0.1.0.0 - 2026-11-13

First public release.

### Added
- Capture page `/bill-capture` (mobile friendly, no JavaScript framework) and menu *Accounting > Vendors > Capture a bill*.
  PDF, JPEG, PNG; HEIC is refused with a hint; size, count and time limits.
- Button *Read with AI* on draft vendor bills; strict JSON validation of the provider answer (never evaluated).
- Proposal: supplier (tax number, IBAN, fuzzy name), header, all positions, taxes from existing purchase taxes,
  account/product/tax from the supplier history, plausibility checks (sum, duplicate number, dates).
- Operating mode per company: Off / Manual / Automatic (queue job, 5 drafts per run, at most 3 attempts;
  fills the draft only, never posts, pays or mails).
- List *Bills to review* with a hints column and filters.
- Vendor prices on the product (`product.supplierinfo`) with undo, last bill and price in the vendor list.
- Selling price warning and the wizard *Adjust selling price...*.
- Log of every reading and price change.
- German translation (`de.po`), English source.
- Tests with a scripted provider (no network, no external AI service).

### Fixed during the visual review (before release)
- The required *Bill Date* blocked every header button of a fresh draft ("Missing required fields"), so *Read
  with AI* did nothing. With capture switched on the date is no longer required on the draft form (posting still
  needs it).
- The capture page ignored the user's language when the website module is installed (it followed the website
  language); it now uses the language of the user. Larger tap targets, readable links and result cards.
- *Bills to review*: one readable *Hints* column instead of eight narrow checkbox columns, positive totals,
  hints first so they stay visible on a phone.
- Margin warnings are listed in the hint box of the draft, not only in a hidden list column.
- The "several taxes with the same rate" hint names at most four taxes (charts with a dozen 20 % taxes made it
  unreadable).
- The proposal box uses a responsive grid (two columns on phones) instead of a wide table.

### Requires
- `ai_translation_consensus` 19.0.1.3.0 or newer (provider purposes and *read documents*).

### Notes
- Hook for a later feature (create a bill from a bank statement line) is documented in the README; not implemented.
