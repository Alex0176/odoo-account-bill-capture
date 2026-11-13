# Vendor Bill Capture with AI (`account_bill_capture`)

Odoo 19 Community. License AGPL-3. Author: Biricon IT Services GmbH.

Photograph or upload a vendor bill, let an AI provider read it, review the proposal, post it yourself.

## What it does

* **Capture.** A mobile friendly page (`/bill-capture`, menu *Accounting > Vendors > Capture a bill*) takes
  one or several photos or PDF files (`<input type=file capture=environment multiple>`, no JavaScript). Each
  file becomes a draft vendor bill in the purchase journal with the file attached. Choose *Only store* or
  *Read with AI*. Drafts created by the mail alias of the purchase journal are covered as well.
* **Preview.** The standard attachment preview of the bill form shows the document next to the draft.
* **Read with AI.** The button *Read with AI* on a draft (accounting group only) sends the attachment to
  the first available provider that has the purpose *Invoices - manual*. The answer is JSON that is
  validated strictly (never evaluated). It is written into the draft as a **proposal**:
  * supplier: 1. tax number, 2. IBAN, 3. fuzzy name. Nothing is created without a click (*Create supplier*);
  * header: number, date, due date, currency, payment reference, bank account (only existing ones);
  * **all positions** as invoice lines (text, quantity, unit, net unit price, discount, tax);
  * taxes only from the **existing** purchase taxes by rate; several taxes with the same rate are decided by
    the history of the supplier, otherwise you get a hint instead of a guess;
  * product, account and tax preferably from the **history of the supplier** (last posted bills with a similar
    text), otherwise from the vendor price list, the article number or the name;
  * plausibility: sum of the positions against the totals (tolerance 0.02), duplicate invoice number per
    supplier, implausible dates. Hints are shown on the draft (`AI hints`), in the chatter (provider and
    confidence per field) and in the list *Bills to review*.
* **Automatic mode.** With *Automatic* a background job (5 drafts per run, time limit, at most 3 attempts)
  reads new drafts that have an attachment, using providers with the purpose *Invoices - automatic*.
  **Automatic only fills the draft. It never posts, pays or sends anything.** A failing or paused provider
  leaves the draft untouched with a hint. The attachment goes to one provider only; retries stay with it.
* **Positions without a product** (setting per company):
  *Only suggest* (default; hint *Product missing* and a button per line), *Create products automatically*
  (goods and services only; never stocked, not for sale, not published on a website, vendor entry with the
  article number and net price, flagged *Created by AI, to review*; an existing product is always searched
  first) or *Treat as general line* (text and expense account, optionally a collective product per expense
  account). Running costs (electricity, rent, insurance ...) are always general lines.
* **Vendor prices** (`product.supplierinfo`, the standard vendor list of the product). Per company: off, when
  the bill is posted (default) or immediately with the proposal. A different price corrects the matching entry
  (vendor, variant, quantity tier, currency, unit, discount); a new vendor gets an additional entry; other
  vendors are never overwritten. Credit notes, zero prices, down payments and rounding lines are ignored.
  Changes larger than a limit (default 50 %) are only suggested and need a click. The cost of stocked products
  is never changed. Every change is noted in the chatter of the product and logged; the button *Undo price
  changes* restores the previous state. The vendor list shows the last bill and price.
* **Selling price warning.** After a new purchase price a line whose product sells below the purchase price
  or below a minimum margin (default 10 %, 0 = loss only, switchable) gets a hint, the bill is flagged
  *Selling price too low*, and the product gets a chatter note (optionally an activity, never an e-mail).
  The selling price is never changed automatically; *Adjust selling price...* opens a wizard with a proposal.
* **Log.** Every reading (provider, time, shortened raw answer, errors) and every price change is stored in
  *Accounting > Configuration > Bill capture log*.

## Setup

1. Install `ai_translation_consensus` **19.0.1.3.0 or newer** (this module needs its provider *purposes*
   and the *read documents* ability) and `account_bill_capture`.
2. *Settings > AI Translation > Providers*: open a provider that reads documents (Anthropic, Gemini,
   OpenAI-compatible with a vision model), set its **purposes** (*Invoices - manual* and/or *Invoices -
   automatic*) and, for an external service, switch on **Allow invoices to leave the server**.
3. *Settings > Accounting*: choose the mode (*Off / Manual / Automatic*) per company and the other options.
4. Users need the group *Accounting / Invoicing* (`account.group_account_invoice`).

## Privacy

Invoices contain data of third parties (suppliers, bank accounts, tax numbers). The document is sent to
**one** configured provider only. An external service is only used for invoices when you explicitly allow it
on the provider (*Allow invoices to leave the server*, default off); a provider on your own infrastructure
(`Runs on own infrastructure`) needs no switch. Check the terms of the service (retention, training) first.

## Security

* Rights: accounting group (and administrators) only, in the controller, the buttons and the Python code.
* Provider keys are never written to the log, the chatter or the hints.
* Limits: PDF, JPEG, PNG (decided by the content, HEIC is refused with a hint), size limit (system parameter
  `account_bill_capture.max_file_mb`, default 10), time limit per reading, at most 3 files read per upload,
  field length limits.
* Prompt injection: the text of the invoice is data. The answer is only taken over as validated data; it can
  not trigger an action. Unknown keys are dropped.

## Limits

* No OCR of its own; the quality depends on the provider. Always compare the proposal with the document.
* PDFs go to the provider as a file when it supports that (Anthropic, Gemini, OpenAI and OpenRouter). Other
  OpenAI-compatible services get the first 5 pages as PNG images (needs `pdftoppm` from poppler-utils).
* The tax is only proposed from rates of existing purchase taxes; special cases (reverse charge, mixed
  rates) are left to the reviewer. Country specific rules are not built in.
* The vendor price of a product is matched per vendor, quantity tier, currency and unit; the unit text of the
  invoice is only used when it equals the name of a unit of measure.

* With capture switched on, *Bill Date* is not required on the draft form (otherwise Odoo blocks every header
  button, including *Read with AI*, until a date is typed). Posting still needs the date.

## Hook for later work (part C, not implemented)

A bank statement line could offer *Create bill*. Hook: `account.move._bill_capture_store(filename, raw,
ai=False)` creates the draft with the attachment; a later module only has to pass the statement line's
partner and amount into the draft and call `_bill_ai_run("invoice_manual", "manual")`.

## Tests

`odoo-bin -d <test db> -i account_bill_capture --test-enable --test-tags /account_bill_capture` (with a
mocked provider; no network, no external AI service is called).
