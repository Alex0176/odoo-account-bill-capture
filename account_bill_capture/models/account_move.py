# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import base64
import json
import logging
import re
import time
from datetime import timedelta

from markupsafe import Markup, escape

from odoo import Command, _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.modules import module as odoo_module

from ..engine import files as file_engine
from ..engine import matching, prompts, schema
from .account_bill_capture_log import RAW_LIMIT

_logger = logging.getLogger(__name__)

RIGHTS_GROUP = "account.group_account_invoice"
PURPOSE_MANUAL = "invoice_manual"
PURPOSE_AUTO = "invoice_auto"
MAX_FILE_MB_PARAM = "account_bill_capture.max_file_mb"
DEFAULT_MAX_MB = 10
CALL_TIMEOUT = 90               # seconds a single reading may take
CRON_BATCH = 5                  # drafts per run of the background job
CRON_SECONDS = 240              # no new reading starts after this many seconds
MAX_ATTEMPTS = 3                # automatic readings per draft that fail
QUEUE_DAYS = 3                  # a queued draft that stays unread is given up after this
NO_ATTACHMENT_HOURS = 24
HISTORY_LINES = 200
HISTORY_THRESHOLD = 0.8
PARTNER_THRESHOLD = 0.88
PRODUCT_THRESHOLD = 0.9
TOLERANCE = 0.02
NAME_LIMIT = 200
CLEAN_URL = re.compile(r"[^\w .,;:()/&+'-]", re.U)


def _clean_name(text, limit=NAME_LIMIT):
    return (CLEAN_URL.sub("", text or "").strip())[:limit]


class AccountMove(models.Model):
    _inherit = "account.move"

    bill_capture_mode = fields.Selection(related="company_id.bill_capture_mode", string="Bill capture mode")
    bill_ai_state = fields.Selection([
        ("queued", "Queued for automatic reading"), ("done", "Proposal made"),
        ("failed", "Reading failed"), ("skipped", "Not read"),
    ], string="AI reading", copy=False, index=True)
    bill_ai_attempts = fields.Integer(copy=False)
    bill_ai_provider_id = fields.Many2one("ai.translation.provider", copy=False, ondelete="set null",
                                          help="The one provider that received the document.")
    bill_ai_provider_name = fields.Char(copy=False)
    bill_ai_date = fields.Datetime(string="Read at", copy=False)
    bill_ai_json = fields.Text(copy=False, groups="account.group_account_invoice")
    bill_ai_notes = fields.Html(string="AI hints", copy=False, sanitize=False,
                                groups="account.group_account_invoice")
    bill_ai_error = fields.Char(copy=False)
    bill_ai_sum_mismatch = fields.Boolean(string="Sum differs", copy=False, index=True)
    bill_ai_partner_unknown = fields.Boolean(string="Supplier unknown", copy=False, index=True)
    bill_ai_tax_unclear = fields.Boolean(string="Tax unclear", copy=False, index=True)
    bill_ai_duplicate = fields.Boolean(string="Possible duplicate", copy=False, index=True)
    bill_ai_product_missing = fields.Boolean(string="Product missing", copy=False, index=True)
    bill_ai_new_product = fields.Boolean(string="New product to review", copy=False, index=True)
    bill_ai_low_margin = fields.Boolean(string="Selling price too low", copy=False, index=True)
    bill_ai_price_pending = fields.Boolean(string="Price change to confirm", copy=False, index=True)
    bill_ai_price_applied = fields.Boolean(string="Vendor prices changed", copy=False)
    bill_ai_flags_text = fields.Char(string="Hints", compute="_compute_bill_ai_flags_text")

    @api.depends("bill_ai_sum_mismatch", "bill_ai_partner_unknown", "bill_ai_tax_unclear", "bill_ai_duplicate",
                 "bill_ai_product_missing", "bill_ai_new_product", "bill_ai_low_margin", "bill_ai_price_pending",
                 "bill_ai_state")
    def _compute_bill_ai_flags_text(self):
        names = [(name, self._fields[name].string) for name in (
            "bill_ai_sum_mismatch", "bill_ai_partner_unknown", "bill_ai_tax_unclear", "bill_ai_duplicate",
            "bill_ai_product_missing", "bill_ai_new_product", "bill_ai_low_margin", "bill_ai_price_pending")]
        for move in self:
            parts = [label for name, label in names if move[name]]
            if move.bill_ai_state == "failed":
                parts.insert(0, _("Reading failed"))
            elif move.bill_ai_state == "queued":
                parts.insert(0, _("Queued"))
            move.bill_ai_flags_text = ", ".join(parts)

    # ------------------------------------------------------------------ helpers
    def _bill_ai_check_rights(self):
        if not (self.env.su or self.env.user.has_group(RIGHTS_GROUP)):
            raise AccessError(_("Only the accounting group may capture bills with AI."))

    def _bill_ai_is_candidate(self):
        self.ensure_one()
        return self.move_type == "in_invoice" and self.state == "draft"

    @api.model_create_multi
    def create(self, vals_list):
        moves = super().create(vals_list)
        skip = self.env.context.get("bill_capture_skip_auto")
        for move in moves:
            if move._bill_ai_is_candidate() and move.company_id.bill_capture_mode == "auto":
                move.with_context(mail_notrack=True).bill_ai_state = "skipped" if skip else "queued"
        return moves

    @api.model
    def _bill_max_bytes(self):
        try:
            mb = float(self.env["ir.config_parameter"].sudo().get_param(MAX_FILE_MB_PARAM, DEFAULT_MAX_MB))
        except ValueError:
            mb = DEFAULT_MAX_MB
        return int(max(mb, 0.001) * 1024 * 1024)

    def _bill_ai_pick_attachment(self):
        """``(attachment, problem)``: the first attachment of the draft that may be sent (PDF, JPEG, PNG,
        within the size limit), or None with the reason."""
        self.ensure_one()
        limit = self._bill_max_bytes()
        problems = []
        attachments = self.env["ir.attachment"].search(
            [("res_model", "=", "account.move"), ("res_id", "=", self.id), ("type", "=", "binary")], order="id")
        # the main attachment first: it is what the preview shows
        attachments = (self.message_main_attachment_id & attachments) | attachments
        for att in attachments:
            if att.file_size and att.file_size > limit:
                problems.append(_("%(name)s is larger than %(mb)s MB.", name=att.name, mb=round(limit / 1048576, 1)))
                continue
            kind = file_engine.detect(att.raw)
            if kind == "heic":
                problems.append(_("%s is a HEIC/HEIF photo. Please take or save it as JPEG (iPhone: Settings, "
                                  "Camera, Formats, Most Compatible).", att.name))
            elif kind is None:
                problems.append(_("%s is not a PDF, JPEG or PNG file.", att.name))
            else:
                return att, ""
        return None, " ".join(problems) or _("The draft has no attachment to read.")

    # ------------------------------------------------------------------ entry points
    def action_bill_ai_read(self):
        """Button 'Read with AI' (draft vendor bill, accounting group)."""
        self.ensure_one()
        self._bill_ai_check_rights()
        result = self._bill_ai_run(PURPOSE_MANUAL, "manual")
        ok = result["state"] == "done"
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Read with AI"), "message": result["message"],
            "type": "success" if ok else "warning", "sticky": not ok,
            "next": {"type": "ir.actions.client", "tag": "soft_reload"}}}

    @api.model
    def _cron_bill_capture(self):
        """Automatic mode: read queued drafts with a provider of the purpose 'Invoices - automatic'.
        Small portions, time limit. It fills drafts only: nothing is posted, paid or sent."""
        started = time.monotonic()
        now = fields.Datetime.now()
        domain = [("bill_ai_state", "=", "queued"), ("state", "=", "draft"), ("move_type", "=", "in_invoice"),
                  ("company_id.bill_capture_mode", "=", "auto")]
        # leave the ones that were given up on long ago
        for move in self.search(domain + [("create_date", "<", now - timedelta(days=QUEUE_DAYS))], limit=50):
            move._bill_ai_give_up(_("No reading was possible within %s days.", QUEUE_DAYS))
        moves = self.search(domain, order="id", limit=CRON_BATCH)
        done = 0
        for move in moves:
            if time.monotonic() - started > CRON_SECONDS:
                break
            try:
                with self.env.cr.savepoint():
                    move.with_company(move.company_id)._bill_ai_cron_one()
            except Exception:  # noqa: BLE001
                _logger.exception("Bill capture: draft %s failed", move.id)
            done += 1
            if not odoo_module.current_test:
                self.env["ir.cron"]._commit_progress(1)
        return done

    def _bill_ai_cron_one(self):
        self.ensure_one()
        attachment, problem = self._bill_ai_pick_attachment()
        if not attachment:
            waited = fields.Datetime.now() - self.create_date
            if waited > timedelta(hours=NO_ATTACHMENT_HOURS) or "HEIC" in problem or "larger" in problem:
                self._bill_ai_give_up(problem)
            return          # the attachment may still arrive
        self._bill_ai_run(PURPOSE_AUTO, "auto")

    def _bill_ai_give_up(self, message):
        self.ensure_one()
        self.write({"bill_ai_state": "failed", "bill_ai_error": message[:250],
                    "bill_ai_notes": self._bill_ai_error_html(message)})

    # ------------------------------------------------------------------ the reading
    def _bill_ai_error_html(self, message):
        return Markup('<div class="alert alert-danger" role="alert"><b>%s</b> %s</div>') % (
            _("The AI reading did not work."), message)

    def _bill_ai_run(self, code, origin):
        """One reading of this draft. Returns ``{"state": done|failed|wait, "message": text}``.

        The attachment goes to one provider only. Errors are stored on the draft (which stays
        otherwise untouched) and in the log; nothing is rolled back, so the pause logic of the
        provider and the log survive."""
        self.ensure_one()
        company = self.company_id
        Provider = self.env["ai.translation.provider"]
        if not self._bill_ai_is_candidate():
            raise UserError(_("Only a draft vendor bill can be read."))
        if company.bill_capture_mode == "off":
            raise UserError(_("Bill capture with AI is switched off for this company "
                              "(Settings, Accounting)."))
        if not hasattr(Provider, "_pick_for_documents"):
            raise UserError(_("The module AI Translation Consensus 19.0.1.3.0 or newer is needed "
                              "(purposes and reading of documents)."))
        attachment, problem = self._bill_ai_pick_attachment()
        if not attachment:
            return self._bill_ai_failed(problem, origin, count=origin == "auto")
        providers = Provider._pick_for_documents(code)
        provider = providers[:1]
        if origin == "auto" and self.bill_ai_provider_id:
            # the document already went to one provider: it stays with that one
            provider = self.bill_ai_provider_id.sudo()
            blocker = provider._documents_blocker(code)
            if blocker:
                return {"state": "wait", "message": _("%(name)s is not available: %(reason)s",
                                                      name=provider.name, reason=blocker)}
        if not provider:
            reasons = "; ".join("%s: %s" % item for item in Provider._documents_overview(code))
            message = _("No provider is available for this purpose.")
            if reasons:
                message += " " + reasons
            if origin == "auto":
                return {"state": "wait", "message": message}
            return self._bill_ai_failed(message, origin, count=False)
        provider = provider.sudo()
        prompt_system, prompt_user = prompts.build(company.name, fields.Date.context_today(self).isoformat())
        files = [{"mimetype": attachment.mimetype if attachment.mimetype in ("application/pdf", "image/jpeg", "image/png")
                  else file_engine.ALLOWED[file_engine.detect(attachment.raw)],
                  "data_b64": base64.b64encode(attachment.raw).decode(), "name": attachment.name or "bill"}]
        self.sudo().write({"bill_ai_provider_id": provider.id, "bill_ai_provider_name": provider.name})
        started = time.monotonic()
        log_vals = {"kind": "read", "move_id": self.id, "company_id": company.id, "origin": origin,
                    "provider_id": provider.id, "provider_name": provider.name,
                    "attachment_name": attachment.name}
        key = provider._config().get("api_key") or ""
        try:
            raw_answer = provider._read_document(prompt_system, prompt_user, files, code, timeout=CALL_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            message = self._bill_ai_scrub(str(exc), key)
            self.env["account.bill.capture.log"].sudo().create(dict(
                log_vals, state="error", error=message[:250], seconds=time.monotonic() - started))
            return self._bill_ai_failed(message, origin, count=origin == "auto")
        seconds = time.monotonic() - started
        raw_answer = self._bill_ai_scrub(raw_answer or "", key)
        try:
            data, problems = schema.validate(raw_answer)
        except schema.BillSchemaError as exc:
            self.env["account.bill.capture.log"].sudo().create(dict(
                log_vals, state="invalid", error=str(exc)[:250], raw_response=raw_answer[:RAW_LIMIT], seconds=seconds))
            return self._bill_ai_failed(_("The answer of %(name)s could not be used: %(why)s",
                                          name=provider.name, why=str(exc)), origin, count=origin == "auto")
        log = self.env["account.bill.capture.log"].sudo().create(dict(
            log_vals, state="ok", raw_response=raw_answer[:RAW_LIMIT], seconds=seconds,
            problems="\n".join(problems) or False))
        try:
            with self.env.cr.savepoint():
                summary = self._bill_ai_apply(data, provider.name, problems, origin)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("Bill capture: the proposal for move %s could not be applied", self.id)
            log.state = "error"
            log.error = _("The proposal could not be applied: %s", str(exc)[:150])
            return self._bill_ai_failed(log.error, origin, count=origin == "auto")
        self.write({"bill_ai_state": "done", "bill_ai_error": False, "bill_ai_date": fields.Datetime.now()})
        return {"state": "done", "message": summary}

    @staticmethod
    def _bill_ai_scrub(text, key):
        text = text or ""
        return text.replace(key, "***") if key else text

    def _bill_ai_failed(self, message, origin, count=False):
        """The draft stays as it is; the reason is shown on it."""
        self.ensure_one()
        vals = {"bill_ai_error": message[:250], "bill_ai_notes": self._bill_ai_error_html(message)}
        if count:
            attempts = self.bill_ai_attempts + 1
            vals["bill_ai_attempts"] = attempts
            vals["bill_ai_state"] = "failed" if attempts >= MAX_ATTEMPTS else "queued"
        else:
            vals["bill_ai_state"] = "failed"
        self.write(vals)
        return {"state": "failed", "message": message}

    # ------------------------------------------------------------------ the proposal
    def _bill_ai_apply(self, data, provider_name, problems, origin):
        """Write the proposal into the draft. Returns a short text for the user. Posts nothing."""
        self.ensure_one()
        company = self.company_id
        if origin == "auto":
            self = self.with_context(lang=company.partner_id.lang or "en_US")
        self = self.with_company(company).with_context(mail_notrack=True, mail_post_autofollow=False, mail_create_nosubscribe=True,
                                 mail_auto_subscribe_no_notify=True)
        warn, info, flags = [], [], {}
        confidence = data["confidence"]
        today = fields.Date.context_today(self)

        # supplier
        partner, how, ambiguous = self._bill_ai_match_partner(data["supplier"])
        flags["bill_ai_partner_unknown"] = False
        if partner and not ambiguous:
            if not self.partner_id:
                self.partner_id = partner
                info.append(_("Supplier %(name)s found (%(how)s).", name=partner.display_name, how=how))
            elif self.partner_id.commercial_partner_id != partner.commercial_partner_id:
                warn.append(_("The bill names %(read)s but the draft has %(have)s.",
                              read=partner.display_name, have=self.partner_id.display_name))
        elif ambiguous:
            flags["bill_ai_partner_unknown"] = not self.partner_id
            warn.append(_("Several suppliers fit %(name)s (%(how)s): please choose one.",
                          name=data["supplier"]["name"] or "?", how=how))
        elif not self.partner_id:
            flags["bill_ai_partner_unknown"] = True
            warn.append(_("Supplier %(name)s is not in the contacts. Use the button 'Create supplier' "
                          "after checking the data.", name=data["supplier"]["name"] or "?"))
        partner = self.partner_id.commercial_partner_id if self.partner_id else self.env["res.partner"]

        # header
        header = {}
        bad_dates = schema.date_problems(data, today)
        if "invoice_date" in bad_dates:
            warn.append(_("The invoice date %s is not plausible; it was not taken over.", data["invoice_date"]))
        elif data["invoice_date"] and not self.invoice_date:
            header["invoice_date"] = data["invoice_date"]
        if "due_date" in bad_dates:
            warn.append(_("The due date %s is before the invoice date; it was not taken over.", data["due_date"]))
        elif data["due_date"]:
            header["invoice_payment_term_id"] = False
            header["invoice_date_due"] = data["due_date"]
        if data["invoice_number"] and not self.ref:
            header["ref"] = data["invoice_number"]
        if data["payment_reference"] and not self.payment_reference:
            header["payment_reference"] = data["payment_reference"]
        if data["currency"] and self.currency_id.name != data["currency"]:
            currency = self.env["res.currency"].with_context(active_test=False).search(
                [("name", "=", data["currency"])], limit=1)
            if currency and currency.active:
                header["currency_id"] = currency.id
            elif currency:
                warn.append(_("The currency %s is not active; activate it before posting.", data["currency"]))
            else:
                warn.append(_("The currency %s is unknown.", data["currency"]))
        if header:
            self.write(header)
        if partner and data["supplier"]["iban"]:
            bank = self.env["res.partner.bank"].search([
                ("partner_id", "child_of", partner.id),
                ("sanitized_acc_number", "=", matching.normalize_iban(data["supplier"]["iban"]))], limit=1)
            if bank:
                self.partner_bank_id = bank
            else:
                info.append(_("The IBAN %s is not stored at the supplier (not created).", data["supplier"]["iban"]))

        # positions
        flags["bill_ai_tax_unclear"] = False
        self._bill_ai_apply_lines(data, partner, warn, info, flags)

        # plausibility
        flags["bill_ai_sum_mismatch"] = False
        labels = {"net": _("The positions add up to a different net amount than the total (%s)."),
                  "gross": _("The positions with tax add up to a different gross amount than the total (%s)."),
                  "sum": _("Net plus tax is not the gross total (%s).")}
        for code, detail in schema.check_totals(data, TOLERANCE):
            if code == "no_totals":
                info.append(_("No totals were read, the sums could not be checked."))
            else:
                flags["bill_ai_sum_mismatch"] = True
                warn.append(labels[code] % detail)
        if data["total_gross"] is not None and self.invoice_line_ids and self.currency_id.name == (
                data["currency"] or self.currency_id.name):
            if abs(self.amount_total - data["total_gross"]) > TOLERANCE and not flags["bill_ai_tax_unclear"]:
                flags["bill_ai_sum_mismatch"] = True
                warn.append(_("The total of the draft (%(draft)s) differs from the bill (%(bill)s).",
                              draft=self.amount_total, bill=data["total_gross"]))
        flags["bill_ai_duplicate"] = False
        duplicates = self._bill_ai_duplicates(partner, self.ref or data["invoice_number"])
        if duplicates:
            flags["bill_ai_duplicate"] = True
            warn.append(_("The invoice number %(ref)s exists already for this supplier: %(moves)s.",
                          ref=self.ref or data["invoice_number"], moves=", ".join(duplicates.mapped("display_name"))))
        for problem in problems:
            info.append(problem)

        # vendor prices and selling prices
        if company.bill_supplier_price_update == "instant":
            self._bill_update_supplier_prices("instant")
        if company.bill_margin_check:
            self._bill_check_margins()
            for line in self.invoice_line_ids.filtered("bill_ai_margin_warning"):
                warn.append(_('Position "%(name)s": %(hint)s.', name=_clean_name(line.name or "", 60),
                              hint=line.bill_ai_margin_hint))

        flags["bill_ai_json"] = json.dumps(data)
        flags["bill_ai_notes"] = self._bill_ai_notes_html(data, provider_name, warn, info, confidence)
        self.write(flags)
        self._bill_ai_refresh_flags()
        self.message_post(
            body=self._bill_ai_notes_html(data, provider_name, warn, info, confidence, chatter=True),
            message_type="comment", subtype_xmlid="mail.mt_note", partner_ids=[])
        return _("Proposal made: %(lines)s positions, %(warn)s hints.", lines=len(data["lines"]), warn=len(warn))

    def _bill_ai_notes_html(self, data, provider_name, warn, info, confidence, chatter=False):
        def conf(key):
            value = confidence.get(key)
            return " (%d %%)" % round(value * 100) if value is not None else ""

        rows = [
            (_("Supplier"), "%s%s" % (data["supplier"]["name"] or "-", conf("supplier"))),
            (_("Tax number"), data["supplier"]["vat"] or "-"),
            (_("IBAN"), data["supplier"]["iban"] or "-"),
            (_("Invoice number"), "%s%s" % (data["invoice_number"] or "-", conf("invoice_number"))),
            (_("Invoice date"), "%s%s" % (data["invoice_date"] or "-", conf("invoice_date"))),
            (_("Due date"), "%s%s" % (data["due_date"] or "-", conf("due_date"))),
            (_("Currency"), "%s%s" % (data["currency"] or "-", conf("currency"))),
            (_("Positions"), "%s%s" % (len(data["lines"]), conf("lines"))),
            (_("Total net / tax / gross"), "%s / %s / %s%s" % (
                data["total_net"], data["total_tax"], data["total_gross"], conf("totals"))),
        ]
        head = Markup("<p><b>%s</b> %s, %s</p>") % (
            _("AI proposal:"), provider_name, fields.Datetime.to_string(fields.Datetime.now()))
        warn_html = Markup("")
        if warn:
            warn_html = Markup('<div class="alert alert-warning" role="alert"><ul class="mb-0">%s</ul></div>') % \
                Markup("").join(Markup("<li>%s</li>") % w for w in warn)
        info_html = Markup("")
        if info:
            info_html = Markup('<ul class="text-muted">%s</ul>') % Markup("").join(
                Markup("<li>%s</li>") % i for i in info)
        table = Markup("<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(13rem,1fr));"
                       "gap:.5rem 1.5rem;font-size:.9em;margin:.5rem 0'>%s</div>") % Markup("").join(
            Markup("<div><div class='text-muted small'>%s</div><div>%s</div></div>") % (k, v) for k, v in rows)
        note = Markup("<p class='text-muted'>%s</p>") % _(
            "Nothing was posted. Check the proposal against the document, then post the bill yourself.")
        return head + warn_html + info_html + table + note

    def _bill_ai_refresh_flags(self):
        Log = self.env["account.bill.capture.log"].sudo()
        for move in self:
            lines = move.invoice_line_ids
            move.write({
                "bill_ai_product_missing": any(lines.mapped("bill_ai_product_missing")),
                "bill_ai_new_product": any(lines.mapped("product_id.bill_ai_created")),
                "bill_ai_low_margin": any(lines.mapped("bill_ai_margin_warning")),
                "bill_ai_price_pending": bool(Log.search_count(
                    [("move_id", "=", move.id), ("kind", "=", "price"), ("state", "=", "pending")], limit=1)),
                "bill_ai_price_applied": bool(Log.search_count(
                    [("move_id", "=", move.id), ("kind", "=", "price"), ("state", "=", "applied")], limit=1)),
            })

    # ------------------------------------------------------------------ supplier
    def _bill_ai_match_partner(self, sup):
        """``(partner, how, ambiguous)``: 1. tax number, 2. IBAN, 3. name (fuzzy). Never creates."""
        company = self.company_id
        Partner = self.env["res.partner"]
        cdomain = [("company_id", "in", [False, company.id])]

        def unique(partners):
            return partners.commercial_partner_id

        vat = matching.normalize_vat(sup.get("vat"))
        if len(vat) >= 6:
            Partner.flush_model(["vat", "active"])
            self.env.cr.execute(
                "SELECT id FROM res_partner WHERE active AND vat IS NOT NULL "
                "AND upper(regexp_replace(vat, '[^A-Za-z0-9]', '', 'g')) = %s", [vat])
            found = unique(Partner.search(cdomain + [("id", "in", [r[0] for r in self.env.cr.fetchall()])]))
            if len(found) == 1:
                return found, _("tax number"), False
            if len(found) > 1:
                return found[:1], _("tax number"), True
        iban = matching.normalize_iban(sup.get("iban"))
        if len(iban) >= 10:
            banks = self.env["res.partner.bank"].search([("sanitized_acc_number", "=", iban)])
            found = unique(banks.partner_id)
            if len(found) == 1:
                return found, _("IBAN"), False
            if len(found) > 1:
                return found[:1], _("IBAN"), True
        name = sup.get("name")
        key = matching.tokens(name, drop_legal=True)
        if key:
            token = max(key, key=len)
            candidates = Partner.search(cdomain + [("parent_id", "=", False), ("name", "ilike", token)], limit=300)
            best, score, ambiguous = matching.best_match(
                [(p.id, p.name) for p in candidates], name, PARTNER_THRESHOLD)
            if best:
                return Partner.browse(best), _("name, %d %% match", round(score * 100)), ambiguous
        return Partner.browse(), "", False

    def action_bill_create_partner(self):
        """Create the supplier named on the bill (after a click) and fill the draft again."""
        self.ensure_one()
        self._bill_ai_check_rights()
        if self.partner_id:
            raise UserError(_("The draft has a supplier already."))
        data = json.loads(self.bill_ai_json or "{}")
        sup = (data or {}).get("supplier") or {}
        if not sup.get("name"):
            raise UserError(_("No supplier name was read from the document."))
        vals = {"name": _clean_name(sup["name"]), "company_type": "company", "supplier_rank": 1}
        address = sup.get("address") or ""
        match = re.match(r"^(.*?),\s*(\d{4,5})\s+(.+?)(?:,\s*(.+))?$", address)
        if match:
            vals.update(street=match.group(1)[:100], zip=match.group(2), city=match.group(3)[:60])
            if match.group(4):
                country = self.env["res.country"].search(
                    ["|", ("name", "=ilike", match.group(4).strip()), ("code", "=ilike", match.group(4).strip())], limit=1)
                if country:
                    vals["country_id"] = country.id
        elif address:
            vals["street"] = address[:100]
        notes = []
        partner = False
        if sup.get("vat"):
            try:
                with self.env.cr.savepoint():
                    partner = self.env["res.partner"].sudo().create(dict(vals, vat=sup["vat"]))
            except ValidationError:
                notes.append(_("The tax number %s was not accepted and was left out.", sup["vat"]))
        if not partner:
            partner = self.env["res.partner"].sudo().create(vals)
        if sup.get("iban"):
            try:
                with self.env.cr.savepoint():
                    self.env["res.partner.bank"].sudo().create({"partner_id": partner.id, "acc_number": sup["iban"]})
            except (ValidationError, UserError):
                notes.append(_("The IBAN %s was not accepted and was left out.", sup["iban"]))
        self.partner_id = partner
        self.message_post(body=_("Supplier %s created from the AI reading. Please check the contact.",
                                 partner.display_name) + (" " + " ".join(notes) if notes else ""),
                          subtype_xmlid="mail.mt_note", partner_ids=[])
        data_clean, problems = schema.validate(self.bill_ai_json)
        with self.env.cr.savepoint():
            self._bill_ai_apply(data_clean, self.bill_ai_provider_name or "AI", problems, "manual")
        return True

    def _bill_ai_duplicates(self, partner, ref):
        self.ensure_one()
        if not partner or not ref:
            return self.browse()
        key = re.sub(r"\s+", "", ref).upper()
        candidates = self.search([
            ("move_type", "=", "in_invoice"), ("commercial_partner_id", "=", partner.id),
            ("id", "!=", self.id), ("state", "!=", "cancel"), ("ref", "!=", False)])
        return candidates.filtered(lambda m: re.sub(r"\s+", "", m.ref or "").upper() == key)

    # ------------------------------------------------------------------ positions
    def _bill_ai_history(self, partner):
        """Lines of the last posted bills of this supplier: what was booked for which text."""
        if not partner:
            return []
        lines = self.env["account.move.line"].search([
            ("move_id.move_type", "=", "in_invoice"), ("move_id.state", "=", "posted"),
            ("move_id.commercial_partner_id", "=", partner.id), ("display_type", "=", "product"),
            ("company_id", "=", self.company_id.id), ("move_id", "!=", self.id)],
            order="date desc, id desc", limit=HISTORY_LINES)
        return [{"id": l.id, "name": l.name or "", "product": l.product_id, "account": l.account_id,
                 "taxes": l.tax_ids} for l in lines]

    def _bill_ai_fallback_account(self, partner):
        """An expense account for a line without usable history: what Odoo itself would propose for this
        supplier, else the default account of the journal. Without one the line could not be saved."""
        Account = self.env["account.account"]
        account = Account.browse()
        if partner:
            account_id = Account._get_most_frequent_account_for_partner(
                self.company_id.id, partner.id, "in_invoice")
            account = Account.browse(account_id).exists() if account_id else account
        if not account:
            account = self.journal_id.default_account_id
        if not account:  # the expense account Odoo proposes for products without a category setting
            category = self.env["product.category"].with_company(self.company_id).search([], limit=1)
            account = category.property_account_expense_categ_id
        if not account:  # last resort: any expense account of the company, so the draft can be saved
            account = Account.search([("account_type", "in", ("expense", "expense_direct_cost")),
                                      ("company_ids", "in", self.company_id.ids)], limit=1)
        return account

    def _bill_ai_purchase_taxes(self, rate):
        return self.env["account.tax"].search([
            ("type_tax_use", "=", "purchase"), ("amount_type", "=", "percent"),
            ("company_id", "=", self.company_id.id), ("price_include", "=", False)]).filtered(
            lambda t: abs(t.amount - rate) < 0.001)

    def _bill_ai_tax_for(self, rate, hist_taxes):
        """``(tax records, problem)`` from the EXISTING purchase taxes by rate; the history of the
        supplier decides between several. No guessing: unclear gives no tax and a problem text."""
        if rate is None:
            return self.env["account.tax"], _("tax rate not read")
        candidates = self._bill_ai_purchase_taxes(rate)
        from_history = hist_taxes & candidates
        if len(from_history) == 1:
            return from_history, ""
        if len(candidates) == 1:
            return candidates, ""
        if not candidates:
            return self.env["account.tax"], _("no purchase tax with %s %%", rate)
        return self.env["account.tax"], _("several purchase taxes with %(rate)s %% (%(names)s)",
                                          rate=rate, names=", ".join(candidates[:4].mapped("name"))
                                          + (", ..." if len(candidates) > 4 else ""))

    def _bill_ai_find_product(self, text, item_number, partner, hist_product=None):
        """An EXISTING product for the position: history, vendor list, article number, name. Or empty."""
        Product = self.env["product.product"]
        company_domain = [("purchase_ok", "=", True), ("company_id", "in", [False, self.company_id.id])]
        if hist_product and hist_product.active and hist_product.purchase_ok:
            return hist_product, "history"
        number = (item_number or "").strip().casefold()
        if partner:
            sellers = self.env["product.supplierinfo"].search([("partner_id", "child_of", partner.id)])
            for seller in sellers:
                prod = seller.product_id or (seller.product_tmpl_id.product_variant_id
                                             if seller.product_tmpl_id.product_variant_count == 1 else Product)
                if not prod or not prod.purchase_ok:
                    continue
                if number and (seller.product_code or "").strip().casefold() == number:
                    return prod, "vendor article number"
            named = [(s.id, s.product_name) for s in sellers if s.product_name]
            best, _score, ambiguous = matching.best_match(named, text, PRODUCT_THRESHOLD, drop_legal=False)
            if best and not ambiguous:
                seller = sellers.browse(best)
                prod = seller.product_id or (seller.product_tmpl_id.product_variant_id
                                             if seller.product_tmpl_id.product_variant_count == 1 else Product)
                if prod and prod.purchase_ok:
                    return prod, "vendor product name"
        if number and len(number) >= 3:
            prod = Product.search(company_domain + [("default_code", "=ilike", item_number.strip())], limit=2)
            if len(prod) == 1:
                return prod, "reference"
        tokens = matching.tokens(text)
        if tokens:
            token = max(tokens, key=len)
            if len(token) >= 3:
                candidates = Product.search(company_domain + [("name", "ilike", token)], limit=60)
                best, _score, ambiguous = matching.best_match(
                    [(p.id, p.display_name) for p in candidates] + [(p.id, p.name) for p in candidates],
                    text, PRODUCT_THRESHOLD, drop_legal=False)
                if best and not ambiguous:
                    return Product.browse(best), "name"
        return Product, ""

    def _bill_ai_uom(self, unit):
        if not unit:
            return self.env["uom.uom"]
        return self.env["uom.uom"].search([("name", "=ilike", unit.strip())], limit=1)

    def _bill_ai_apply_lines(self, data, partner, warn, info, flags):
        if not data["lines"]:
            info.append(_("No positions were read; the lines were not touched."))
            return
        own = self.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        if own and not all(own.mapped("bill_ai_generated")):
            warn.append(_("The draft has lines that were entered by hand: the positions of the bill were "
                          "not written. Remove those lines to let the AI fill them."))
            return
        company = self.company_id
        mode = company.bill_unknown_product_mode
        history = self._bill_ai_history(partner)
        commands = [Command.delete(l.id) for l in self.invoice_line_ids.filtered("bill_ai_generated")]
        usual_account = False
        if history:
            counts = {}
            for h in history:
                counts[h["account"].id] = counts.get(h["account"].id, 0) + 1
            usual_account = self.env["account.account"].browse(max(counts, key=counts.get))
        for ld in data["lines"]:
            hist = None
            best, score, _amb = matching.best_match(
                [(h["id"], h["name"]) for h in history], ld["description"], HISTORY_THRESHOLD, drop_legal=False)
            if best:
                hist = next(h for h in history if h["id"] == best)
            product, source = self._bill_ai_find_product(
                ld["description"], ld["item_number"], partner, hist["product"] if hist else None)
            taxes, tax_problem = self._bill_ai_tax_for(ld["tax_rate"], hist["taxes"] if hist else self.env["account.tax"])
            art = ld["art"]
            missing, hints = False, []
            account = hist["account"] if hist else False
            if not product:
                if art == "laufende_kosten" or mode == "general":
                    if company.bill_general_product and (account or usual_account):
                        product = self._bill_ai_general_product(account or usual_account)
                        hints.append(_("General line (running costs)"))
                    else:
                        hints.append(_("General line"))
                    account = account or usual_account
                elif mode == "create" and art in ("ware", "dienstleistung"):
                    product = self._bill_ai_create_product(ld, partner, account or usual_account, art)
                    hints.append(_("Product created by AI, please review"))
                else:
                    missing = True
                    hints.append(_("Product missing"))
                    account = account or usual_account
            if tax_problem:
                flags["bill_ai_tax_unclear"] = True
                hints.append(_("Tax unclear: %s", tax_problem))
                warn.append(_("Position \"%(text)s\": tax unclear (%(why)s).", text=ld["description"][:60], why=tax_problem))
            if source:
                info.append(_("\"%(text)s\": product %(product)s (%(source)s).", text=ld["description"][:60],
                              product=product.display_name, source=source))
            vals = {
                "name": ld["description"], "quantity": ld["quantity"], "price_unit": ld["unit_price_net"],
                "discount": ld["discount_percent"], "tax_ids": [Command.set(taxes.ids)],
                "bill_ai_generated": True, "bill_ai_art": art, "bill_ai_item_number": ld["item_number"],
                "bill_ai_product_missing": missing, "bill_ai_hint": "; ".join(hints) or False,
            }
            if product:
                vals["product_id"] = product.id
            if not account:
                account = self._bill_ai_fallback_account(partner)
            if account:
                vals["account_id"] = account.id
            uom = self._bill_ai_uom(ld["unit"])
            if uom:
                vals["product_uom_id"] = uom.id
            commands.append(Command.create(vals))
        self.write({"invoice_line_ids": commands})

    # ------------------------------------------------------------------ products
    def _bill_ai_general_product(self, account):
        """The collective product 'General expense' of an expense account (a service without stock)."""
        Product = self.env["product.product"].with_context(active_test=False)
        code = "GENERAL-EXPENSE-%s" % account.code
        product = Product.search([("default_code", "=", code), ("company_id", "in", [False, self.company_id.id])], limit=1)
        if product:
            return product
        vals = self._bill_ai_product_defaults()
        vals.update(name=_("General expense %s", account.code), default_code=code, type="service",
                    property_account_expense_id=account.id)
        return Product.sudo().create(vals)

    def _bill_ai_product_defaults(self):
        """Values every product made by the capture has: never for sale, never published, never stocked."""
        fields_ = self.env["product.template"]._fields
        vals = {"sale_ok": False, "purchase_ok": True, "company_id": self.company_id.id}
        if "is_storable" in fields_:
            vals["is_storable"] = False
        if "is_published" in fields_:
            vals["is_published"] = False
        return vals

    def _bill_ai_create_product(self, ld, partner, account, art):
        """A new product for a position, after a search for an existing one (no duplicates).

        Goods and services only; never stocked, not for sale, not published, no variants. The vendor
        article number and the net price go to a vendor entry."""
        found, _source = self._bill_ai_find_product(ld["description"], ld["item_number"], partner)
        if found:
            return found
        vals = self._bill_ai_product_defaults()
        name = _clean_name(ld["description"])
        vals.update(name=name or _("Product from bill"), type="service" if art == "dienstleistung" else "consu",
                    bill_ai_created=True, bill_ai_move_id=self.id,
                    description_purchase=ld["description"][:500])
        if account:
            vals["property_account_expense_id"] = account.id
        history = self._bill_ai_history(partner)
        categ = next((h["product"].categ_id for h in history if h["product"]), False)
        if categ:
            vals["categ_id"] = categ.id
        product = self.env["product.product"].sudo().create(vals)
        if partner:
            price = ld["unit_price_net"] * (1 - (ld.get("discount_percent") or 0.0) / 100.0)
            self.env["product.supplierinfo"].sudo().create({
                "partner_id": partner.id, "product_tmpl_id": product.product_tmpl_id.id,
                "price": price, "currency_id": self.currency_id.id, "min_qty": 1.0,
                "product_code": (ld["item_number"] or False), "product_name": name,
                "company_id": self.company_id.id,
                "sequence": 1,
            })
        product.product_tmpl_id.sudo().message_post(
            body=Markup(_("Created from the vendor bill %s by the AI capture. Please review it.") % self._bill_link()),
            subtype_xmlid="mail.mt_note", partner_ids=[])
        self.env["account.bill.capture.log"].sudo().create({
            "kind": "create", "move_id": self.id, "company_id": self.company_id.id, "state": "applied",
            "product_id": product.product_tmpl_id.id, "partner_id": partner.id if partner else False,
            "new_price": ld["unit_price_net"], "note": name[:200]})
        return product

    def _bill_ai_find_or_create_product(self, line, create, partner, art):
        """For the button on a line: find (and if ``create`` make) the product of this move line."""
        self.ensure_one()
        ld = {"description": line.name or "", "item_number": line.bill_ai_item_number,
              "unit_price_net": line.price_unit, "discount_percent": line.discount or 0.0}
        found, _src = self._bill_ai_find_product(ld["description"], ld["item_number"], partner)
        if found or not create:
            return found
        return self._bill_ai_create_product(ld, partner, line.account_id, art)

    # ------------------------------------------------------------------ upload
    @api.model
    def _bill_capture_store(self, filename, raw, ai=False):
        """The upload path: one draft vendor bill with the file attached; with ``ai`` it is read at once.
        Returns ``(move, message)``. Raises UserError for a file that is not accepted."""
        self._bill_ai_check_rights()
        if len(raw) > self._bill_max_bytes():
            raise UserError(_("%(name)s is larger than %(mb)s MB.", name=filename,
                              mb=round(self._bill_max_bytes() / 1048576, 1)))
        kind = file_engine.detect(raw)
        if kind == "heic":
            raise UserError(_("%s is a HEIC/HEIF photo. Please take or save it as JPEG (iPhone: Settings, "
                              "Camera, Formats, Most Compatible).", filename))
        if not kind:
            raise UserError(_("%s is not a PDF, JPEG or PNG file.", filename))
        company = self.env.company
        journal = self.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", company.id)], limit=1)
        if not journal:
            raise UserError(_("The company has no purchase journal."))
        name = re.sub(r"[^\w .()-]", "_", filename or "bill")[:120] or "bill"
        move = self.create({
            "move_type": "in_invoice", "journal_id": journal.id, "company_id": company.id})
        attachment = self.env["ir.attachment"].create({
            "name": name, "raw": raw, "res_model": "account.move", "res_id": move.id,
            "mimetype": file_engine.ALLOWED[kind]})
        if not move.message_main_attachment_id:
            move.message_main_attachment_id = attachment
        message = _("Stored.")
        if ai:
            if company.bill_capture_mode == "off":
                message = _("Stored. AI reading is switched off for this company.")
            else:
                if move.bill_ai_state == "queued":
                    move.bill_ai_state = False
                result = move._bill_ai_run(PURPOSE_MANUAL, "upload")
                message = result["message"]
        elif move.bill_ai_state == "queued":
            move.bill_ai_state = "skipped"          # "only store" was chosen: the background job leaves it
        return move, message

    def action_bill_review(self):
        """'Review and post' in the list: opens the draft. Posting stays a manual step there."""
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "account.move", "res_id": self.id,
                "view_mode": "form", "views": [(False, "form")], "target": "current"}
