# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
import json

from odoo.exceptions import AccessError, UserError
from odoo.addons.ai_translation_consensus.providers.http import ProviderError, ProviderUnavailable
from odoo.tests.common import tagged

from .common import HEIC, PDF, PNG, BillCase, ScriptedBillProvider, answer

IBAN = "AT611904300234573201"


@tagged("post_install", "-at_install")
class TestCapture(BillCase):
    # ------------------------------------------------------------ supplier
    def test_supplier_by_tax_number(self):
        move = self.new_bill()
        self.read_bill(move, answer(supplier={"name": "Something else", "vat": "ATU 000.000-01"}))
        self.assertEqual(move.partner_id, self.supplier)
        self.assertFalse(move.bill_ai_partner_unknown)
        self.assertEqual(move.bill_ai_state, "done")

    def test_bill_without_date_is_not_posted_into_the_current_year(self):
        move = self.new_bill()
        self.read_bill(move, answer(invoice_date=None))
        self.assertFalse(move.invoice_date)
        with self.assertRaises(UserError):
            move.action_post()
        self.assertEqual(move.state, "draft")

    def test_supplier_by_iban(self):
        self.env["res.partner.bank"].create({"partner_id": self.supplier.id, "acc_number": IBAN})
        move = self.new_bill()
        self.read_bill(move, answer(supplier={"name": "Other name", "iban": "AT61 1904 3002 3457 3201"}))
        self.assertEqual(move.partner_id, self.supplier)
        self.assertEqual(move.partner_bank_id.sanitized_acc_number, IBAN)

    def test_supplier_by_fuzzy_name(self):
        move = self.new_bill()
        self.read_bill(move, answer(supplier={"name": "MUSTER STROM GmbH & Co KG"}))
        self.assertEqual(move.partner_id, self.supplier)
        move = self.new_bill()
        self.read_bill(move, answer(supplier={"name": "Muster-Strom"}))
        self.assertEqual(move.partner_id, self.supplier)

    def test_unknown_supplier_is_not_created_without_a_click(self):
        count = self.env["res.partner"].search_count([])
        move = self.new_bill()
        self.read_bill(move, answer(supplier={"name": "Brandneu Wasser GmbH", "vat": "ATU00000099",
                                              "iban": IBAN, "address": "Hauptstrasse 1, 1010 Wien, Austria"}))
        self.assertFalse(move.partner_id)
        self.assertTrue(move.bill_ai_partner_unknown)
        self.assertEqual(self.env["res.partner"].search_count([]), count)
        self.assertIn("Create supplier", move.bill_ai_notes)
        move.with_user(self.accountant).action_bill_create_partner()
        partner = move.partner_id
        self.assertEqual(partner.name, "Brandneu Wasser GmbH")
        self.assertEqual((partner.zip, partner.city), ("1010", "Wien"))
        self.assertTrue(partner.bank_ids)
        self.assertFalse(move.bill_ai_partner_unknown)
        self.assertTrue(move.invoice_line_ids)

    def test_two_suppliers_with_the_same_tax_number_are_not_guessed(self):
        self.partner_ctx.create({"name": "Muster-Strom Filiale", "is_company": True, "vat": "ATU00000001"})
        move = self.new_bill()
        self.read_bill(move, answer(supplier={"vat": "ATU00000001"}))
        self.assertFalse(move.partner_id)
        self.assertTrue(move.bill_ai_partner_unknown)
        self.assertIn("Several suppliers", move.bill_ai_notes)

    def test_a_supplier_set_by_hand_is_kept(self):
        other = self.partner_ctx.create({"name": "Beispiel Gas AG", "is_company": True})
        move = self.new_bill(partner=other)
        self.read_bill(move)
        self.assertEqual(move.partner_id, other)
        self.assertIn("Muster-Strom", move.bill_ai_notes)

    # ------------------------------------------------------------ header
    def test_header_fields_and_notes(self):
        self.company.currency_id.active = True
        move = self.new_bill()
        self.read_bill(move, answer(invoice_date="2026-03-02", due_date="2026-04-01", payment_reference="RF123"))
        self.assertEqual((move.ref, str(move.invoice_date), str(move.invoice_date_due)),
                         ("RE-2026-001", "2026-03-02", "2026-04-01"))
        self.assertEqual(move.payment_reference, "RF123")
        self.assertIn("90 %", move.bill_ai_notes)                   # confidence per field
        self.assertIn("Scripted reader A", move.bill_ai_notes)
        self.assertEqual(move.state, "draft")
        note = move.message_ids.filtered(lambda m: "AI proposal" in (m.body or ""))
        self.assertTrue(note)
        self.assertIn("Scripted reader A", note[0].body)

    def test_implausible_dates_are_not_taken_over(self):
        move = self.new_bill()
        self.read_bill(move, answer(invoice_date="2019-01-01"))
        self.assertFalse(move.invoice_date)
        self.assertIn("not plausible", move.bill_ai_notes)

    # ------------------------------------------------------------ taxes
    def test_tax_by_rate_when_unique_and_ambiguity_gives_a_hint_instead_of_a_guess(self):
        for tax in self.env["account.tax"].search([("company_id", "=", self.company.id), ("type_tax_use", "=", "purchase")]):
            tax.active = False
        only = self.tax(20.0, "Only 20")
        move = self.new_bill()
        self.read_bill(move)
        self.assertEqual(move.invoice_line_ids.tax_ids, only)
        self.assertFalse(move.bill_ai_tax_unclear)
        self.tax(20.0, "Second 20")
        move = self.new_bill()
        self.read_bill(move)
        self.assertFalse(move.invoice_line_ids.tax_ids)
        self.assertTrue(move.bill_ai_tax_unclear)
        self.assertIn("several purchase taxes", move.bill_ai_notes)

    def test_history_decides_between_taxes_of_the_same_rate(self):
        for tax in self.env["account.tax"].search([("company_id", "=", self.company.id), ("type_tax_use", "=", "purchase")]):
            tax.active = False
        a, b = self.tax(20.0, "Twenty A"), self.tax(20.0, "Twenty B")
        self.post_history(self.supplier, "Elektrische Energie September", taxes=b)
        move = self.new_bill()
        self.read_bill(move)
        self.assertEqual(move.invoice_line_ids.tax_ids, b)
        self.assertFalse(move.bill_ai_tax_unclear)

    def test_a_rate_without_a_tax_is_reported_not_invented(self):
        move = self.new_bill()
        before = self.env["account.tax"].search_count([])
        data = answer(lines=[dict(answer()["lines"][0], tax_rate=7.7)], total_gross=107.7, total_tax=7.7)
        self.read_bill(move, data)
        self.assertEqual(self.env["account.tax"].search_count([]), before)
        self.assertTrue(move.bill_ai_tax_unclear)
        self.assertIn("no purchase tax with 7.7", move.bill_ai_notes)

    # ------------------------------------------------------------ history
    def test_history_gives_product_account_and_tax(self):
        account = self.expense_account("999111")
        tax = self.tax(20.0, "Hist 20")
        product = self.env["product.product"].create({"name": "Strom Arbeitspreis", "purchase_ok": True,
                                                      "type": "service"})
        self.post_history(self.supplier, "Elektrische Energie September", product=product, account=account, taxes=tax)
        move = self.new_bill()
        self.read_bill(move)
        line = move.invoice_line_ids
        self.assertEqual((line.product_id, line.account_id, line.tax_ids), (product, account, tax))
        self.assertIn("history", move.bill_ai_notes)

    def test_without_a_product_the_usual_account_of_the_supplier_is_proposed(self):
        account = self.expense_account("999222")
        self.post_history(self.supplier, "Something completely different", account=account)
        move = self.new_bill()
        self.read_bill(move)
        self.assertEqual(move.invoice_line_ids.account_id, account)
        self.assertFalse(move.invoice_line_ids.product_id)

    def test_all_positions_become_lines_with_quantity_price_and_discount(self):
        lines = [
            {"description": "Widget A", "item_number": "W-1", "quantity": 3, "unit": "Units", "unit_price_net": 10.0,
             "discount_percent": 10, "tax_rate": 20, "art": "ware"},
            {"description": "Montage", "quantity": 2, "unit_price_net": 50.0, "tax_rate": 20, "art": "dienstleistung"},
        ]
        move = self.new_bill()
        self.read_bill(move, answer(lines=lines, total_net=127.0, total_tax=25.4, total_gross=152.4))
        got = move.invoice_line_ids.sorted("id")
        self.assertEqual(len(got), 2)
        self.assertEqual((got[0].quantity, got[0].price_unit, got[0].discount), (3, 10.0, 10))
        self.assertEqual(got[0].product_uom_id.name, "Units")
        self.assertEqual(got[1].quantity, 2)
        self.assertTrue(all(got.mapped("bill_ai_generated")))

    def test_a_new_reading_replaces_only_its_own_lines(self):
        move = self.new_bill()
        self.read_bill(move)
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.read_bill(move)
        self.assertEqual(len(move.invoice_line_ids), 1)
        move.write({"invoice_line_ids": [(0, 0, {"name": "by hand", "price_unit": 5.0})]})
        self.read_bill(move)
        self.assertEqual(len(move.invoice_line_ids), 2)
        self.assertIn("entered by hand", move.bill_ai_notes)

    # ------------------------------------------------------------ plausibility
    def test_sum_difference_is_flagged(self):
        move = self.new_bill()
        self.read_bill(move, answer(total_gross=150.0, total_tax=50.0))
        self.assertTrue(move.bill_ai_sum_mismatch)
        self.assertIn("gross", move.bill_ai_notes)
        move = self.new_bill()
        self.read_bill(move, answer())
        self.assertFalse(move.bill_ai_sum_mismatch)

    def test_the_review_list_names_the_hints_in_one_column(self):
        move = self.new_bill()
        self.read_bill(move, answer(total_gross=150.0, total_tax=50.0))
        self.assertIn("Sum differs", move.bill_ai_flags_text)
        clean = self.new_bill()
        self.read_bill(clean, answer())
        self.assertNotIn("Sum differs", clean.bill_ai_flags_text)

    def test_the_bill_date_is_not_required_on_the_form_while_capture_is_on(self):
        """Odoo blocks every header button on a draft with a required empty field: 'Read with AI' must work."""
        form_view = self.env.ref("account.view_move_form")
        arch = self.env["account.move"].get_view(form_view.id, "form")["arch"]
        self.assertIn("bill_capture_mode == &#39;off&#39;", arch.replace("'", "&#39;"))

    def test_duplicate_invoice_number_per_supplier(self):
        self.post_history(self.supplier, "x", ref="RE-2026-001")
        move = self.new_bill()
        self.read_bill(move)
        self.assertTrue(move.bill_ai_duplicate)
        self.assertIn("exists already", move.bill_ai_notes)
        other = self.partner_ctx.create({"name": "Beispiel Gas AG", "is_company": True, "vat": "ATU00000002"})
        move = self.new_bill()
        self.read_bill(move, answer(supplier={"name": "Beispiel Gas AG", "vat": "ATU00000002"}))
        self.assertFalse(move.bill_ai_duplicate)

    # ------------------------------------------------------------ nothing leaves, nothing is posted
    def test_nothing_is_posted_paid_or_mailed(self):
        mails = self.env["mail.mail"].search_count([])
        notifications = self.env["mail.notification"].search_count([])
        move = self.new_bill()
        self.read_bill(move)
        self.assertEqual(move.state, "draft")
        self.assertEqual(move.payment_state, "not_paid")
        self.assertEqual(self.env["mail.mail"].search_count([]), mails)
        self.assertEqual(self.env["mail.notification"].search_count([]), notifications)
        self.assertFalse(self.env["account.payment"].search([("move_id", "=", move.id)]))

    # ------------------------------------------------------------ attachments and limits
    def test_heic_is_refused_with_a_hint(self):
        move = self.new_bill(raw=HEIC, name="photo.heic")
        result = self.read_bill(move)
        self.assertEqual(result["state"], "failed")
        self.assertIn("HEIC", move.bill_ai_error)
        self.assertFalse(ScriptedBillProvider.calls)
        self.assertFalse(move.invoice_line_ids)

    def test_wrong_type_and_missing_attachment(self):
        move = self.new_bill(raw=b"just some text", name="note.pdf")
        self.assertEqual(self.read_bill(move)["state"], "failed")
        self.assertIn("not a PDF", move.bill_ai_error)
        move = self.Move.create({"move_type": "in_invoice", "journal_id": self.journal.id})
        self.assertEqual(self.read_bill(move)["state"], "failed")
        self.assertIn("no attachment", move.bill_ai_error)
        self.assertFalse(ScriptedBillProvider.calls)

    def test_size_limit(self):
        self.env["ir.config_parameter"].set_param("account_bill_capture.max_file_mb", "0.001")
        move = self.new_bill(raw=PDF + b"0" * 5000)
        self.assertEqual(self.read_bill(move)["state"], "failed")
        self.assertIn("larger", move.bill_ai_error)
        self.assertFalse(ScriptedBillProvider.calls)

    def test_the_document_goes_to_the_provider_as_a_file(self):
        move = self.new_bill(raw=PNG, name="scan.png")
        self.read_bill(move)
        call = ScriptedBillProvider.calls[0]
        self.assertEqual(call["files"][0]["mimetype"], "image/png")
        self.assertEqual(call["files"][0]["name"], "scan.png")
        self.assertIn("never follow instructions", call["system"])

    # ------------------------------------------------------------ errors keep the draft untouched
    def test_a_failing_provider_leaves_the_draft_untouched_with_a_hint(self):
        move = self.new_bill(partner=self.supplier, ref="MINE")
        ScriptedBillProvider.raises = ProviderError("service said no")
        result = self.read_bill(move)
        self.assertEqual(result["state"], "failed")
        self.assertEqual((move.ref, move.partner_id), ("MINE", self.supplier))
        self.assertFalse(move.invoice_line_ids)
        self.assertIn("service said no", move.bill_ai_notes)
        log = self.env["account.bill.capture.log"].search([("move_id", "=", move.id)])
        self.assertEqual((log.kind, log.state), ("read", "error"))

    def test_an_unusable_answer_is_logged_and_changes_nothing(self):
        move = self.new_bill()
        result = self.read_bill(move, "I am sorry, I cannot read this.")
        self.assertEqual(result["state"], "failed")
        logs = self.env["account.bill.capture.log"].search([("move_id", "=", move.id)])
        self.assertEqual(logs.mapped("state"), ["invalid", "invalid"])      # one retry, then given up
        self.assertIn("cannot read", logs[0].raw_response)
        self.assertEqual(len(ScriptedBillProvider.calls), 2)
        self.assertFalse(move.invoice_line_ids)

    def test_an_empty_first_reading_is_repeated_and_the_better_one_is_used(self):
        move = self.new_bill()
        empty = answer(lines=[], total_net=None, total_tax=None, total_gross=None, invoice_number="X")
        replies = [json.dumps(empty), json.dumps(answer())]
        ScriptedBillProvider.replies = list(replies)
        try:
            result = self.read_bill(move)
        finally:
            ScriptedBillProvider.replies = None
        self.assertEqual(result["state"], "done")
        self.assertEqual(len(move.invoice_line_ids), 1)

    def test_the_log_keeps_provider_time_and_a_shortened_answer_without_the_key(self):
        self.provider.sudo().write({"kind": "scripted_bill", "api_key": "sk-secret-123456"})
        move = self.new_bill()
        reply = answer()
        reply["lines"][0]["description"] = "Energie sk-secret-123456 " + "x" * 6000
        self.read_bill(move, reply)
        log = self.env["account.bill.capture.log"].search([("move_id", "=", move.id)])
        self.assertEqual((log.state, log.provider_name), ("ok", "Scripted reader A"))
        self.assertLessEqual(len(log.raw_response), 4000)
        self.assertNotIn("sk-secret-123456", log.raw_response)
        self.assertNotIn("sk-secret-123456", move.bill_ai_notes or "")
        for msg in move.message_ids:
            self.assertNotIn("sk-secret-123456", msg.body or "")
        self.provider.sudo().api_key = False

    def test_usage_of_the_reading_is_recorded_for_the_cost_display(self):
        move = self.new_bill()
        self.read_bill(move)
        usage = self.env["ai.translation.usage"].search([("provider_id", "=", self.provider.id)])
        self.assertEqual((usage.calls, usage.input_tokens, usage.purpose_id.code), (1, 900, "invoice_manual"))

    # ------------------------------------------------------------ providers
    def test_only_one_provider_ever_gets_the_document(self):
        second = self.Provider.create({
            "name": "Scripted reader B", "kind": "scripted_bill", "is_local": True, "sequence": 2,
            "purpose_ids": [(6, 0, self.provider.purpose_ids.ids)]})
        move = self.new_bill()
        ScriptedBillProvider.raises = ProviderError("down")
        self.read_bill(move)
        self.assertEqual([c["name"] for c in ScriptedBillProvider.calls], ["Scripted reader A"])
        second.unlink()

    def test_the_first_available_provider_by_sequence_is_used_and_the_pause_is_respected(self):
        second = self.Provider.create({
            "name": "Scripted reader B", "kind": "scripted_bill", "is_local": True, "sequence": 2,
            "purpose_ids": [(6, 0, self.provider.purpose_ids.ids)]})
        self.provider.write({"paused_until": "2999-01-01 00:00:00", "pause_reason": "quota"})
        move = self.new_bill()
        self.read_bill(move)
        self.assertEqual(ScriptedBillProvider.calls[0]["name"], "Scripted reader B")
        self.provider.write({"paused_until": False, "pause_reason": False})
        move = self.new_bill()
        self.read_bill(move)
        self.assertEqual(ScriptedBillProvider.calls[-1]["name"], "Scripted reader A")
        second.unlink()

    def test_an_external_provider_needs_the_data_protection_switch(self):
        self.provider.is_local = False
        move = self.new_bill()
        result = self.read_bill(move)
        self.assertEqual(result["state"], "failed")
        self.assertIn("data protection", move.bill_ai_error)
        self.assertFalse(ScriptedBillProvider.calls)
        self.provider.allow_documents_external = True
        self.assertEqual(self.read_bill(move)["state"], "done")
        self.provider.write({"is_local": True, "allow_documents_external": False})

    def test_manual_and_automatic_purposes_are_separate(self):
        self.provider.purpose_ids = self.env.ref("ai_translation_consensus.purpose_invoice_manual")
        move = self.new_bill()
        self.assertEqual(self.read_bill(move, code="invoice_auto", origin="auto")["state"], "wait")
        self.assertEqual(self.read_bill(move)["state"], "done")
        self.provider.purpose_ids |= self.env.ref("ai_translation_consensus.purpose_invoice_auto")

    # ------------------------------------------------------------ modes
    def test_mode_off_refuses_the_button_and_queues_nothing(self):
        self.company.bill_capture_mode = "off"
        move = self.new_bill()
        self.assertFalse(move.bill_ai_state)
        with self.assertRaises(UserError):
            move.action_bill_ai_read()
        self.assertFalse(ScriptedBillProvider.calls)

    def test_mode_manual_queues_nothing_and_the_button_works(self):
        move = self.new_bill()
        self.assertFalse(move.bill_ai_state)
        self.env["account.move"]._cron_bill_capture()
        self.assertFalse(ScriptedBillProvider.calls)
        res = move.with_user(self.accountant).action_bill_ai_read()
        self.assertEqual(res["tag"], "display_notification")
        self.assertEqual(move.bill_ai_state, "done")

    def test_mode_automatic_queues_new_drafts_and_the_job_fills_them_without_posting(self):
        self.company.bill_capture_mode = "auto"
        self.provider.purpose_ids = self.env.ref("ai_translation_consensus.purpose_invoice_auto")
        move = self.new_bill()
        self.assertEqual(move.bill_ai_state, "queued")
        self.env["account.move"]._cron_bill_capture()
        self.assertEqual(move.bill_ai_state, "done")
        self.assertEqual(move.partner_id, self.supplier)
        self.assertEqual(move.state, "draft")
        self.assertEqual(self.Move.search_count([("state", "=", "posted"), ("id", "=", move.id)]), 0)
        self.provider.purpose_ids |= self.env.ref("ai_translation_consensus.purpose_invoice_manual")

    def test_the_hook_also_covers_drafts_made_by_the_mail_alias(self):
        self.company.bill_capture_mode = "auto"
        move = self.Move.with_context(default_move_type="in_invoice", default_journal_id=self.journal.id).message_new(
            {"subject": "Rechnung", "body": "<p>see attached</p>", "from": "billing@muster-strom.example",
             "message_id": "<x1@example>", "email_from": "billing@muster-strom.example"},
            {"move_type": "in_invoice", "journal_id": self.journal.id, "company_id": self.company.id})
        self.assertEqual(move.bill_ai_state, "queued")

    def test_the_job_works_in_portions_of_five(self):
        self.company.bill_capture_mode = "auto"
        moves = self.Move
        for _i in range(7):
            moves |= self.new_bill()
        self.assertEqual(self.env["account.move"]._cron_bill_capture(), 5)
        self.assertEqual(len(moves.filtered(lambda m: m.bill_ai_state == "done")), 5)
        self.assertEqual(self.env["account.move"]._cron_bill_capture(), 2)
        self.assertEqual(len(moves.filtered(lambda m: m.bill_ai_state == "done")), 7)

    def test_the_job_leaves_a_failed_draft_untouched_retries_a_limited_number_of_times_then_stops(self):
        self.company.bill_capture_mode = "auto"
        move = self.new_bill(partner=self.supplier)
        ScriptedBillProvider.raises = ProviderError("no good")
        for expected in ("queued", "queued", "failed"):
            self.env["account.move"]._cron_bill_capture()
            self.assertEqual(move.bill_ai_state, expected)
        self.assertEqual(move.bill_ai_attempts, 3)
        self.assertFalse(move.invoice_line_ids)
        calls = len(ScriptedBillProvider.calls)
        self.env["account.move"]._cron_bill_capture()
        self.assertEqual(len(ScriptedBillProvider.calls), calls)         # no more tries
        self.assertIn("no good", move.bill_ai_notes)

    def test_a_paused_provider_means_waiting_not_failing(self):
        self.company.bill_capture_mode = "auto"
        move = self.new_bill()
        ScriptedBillProvider.raises = ProviderUnavailable("usage limit reached", 600)
        self.env["account.move"]._cron_bill_capture()
        self.assertEqual(move.bill_ai_state, "queued")
        self.assertTrue(self.provider.is_paused)
        ScriptedBillProvider.calls = []
        self.env["account.move"]._cron_bill_capture()
        self.assertFalse(ScriptedBillProvider.calls)                       # the provider is left alone
        self.assertEqual(move.bill_ai_state, "queued")
        self.provider.action_resume()

    def test_an_automatic_retry_stays_with_the_provider_that_got_the_document(self):
        self.company.bill_capture_mode = "auto"
        second = self.Provider.create({
            "name": "Scripted reader B", "kind": "scripted_bill", "is_local": True, "sequence": 2,
            "purpose_ids": [(6, 0, self.provider.purpose_ids.ids)]})
        move = self.new_bill()
        ScriptedBillProvider.raises = ProviderError("first fails")
        self.env["account.move"]._cron_bill_capture()
        self.provider.sequence = 5                  # now B would come first, but A already has the document
        ScriptedBillProvider.raises = None
        self.env["account.move"]._cron_bill_capture()
        self.assertEqual({c["name"] for c in ScriptedBillProvider.calls}, {"Scripted reader A"})
        second.unlink()
        self.provider.sequence = 1

    def test_only_store_in_automatic_mode_is_respected(self):
        self.company.bill_capture_mode = "auto"
        move, _message = self.Move.with_user(self.accountant)._bill_capture_store("a.pdf", PDF, ai=False)
        self.assertEqual(move.bill_ai_state, "skipped")
        self.env["account.move"]._cron_bill_capture()
        self.assertFalse(ScriptedBillProvider.calls)

    # ------------------------------------------------------------ rights
    def test_only_the_accounting_group_may_use_it(self):
        move = self.new_bill()
        with self.assertRaises(AccessError):
            move.with_user(self.plain_user).action_bill_ai_read()
        with self.assertRaises(AccessError):
            self.Move.with_user(self.plain_user)._bill_capture_store("a.pdf", PDF)
        self.assertFalse(ScriptedBillProvider.calls)
        move.with_user(self.accountant).action_bill_ai_read()
        self.assertEqual(move.bill_ai_state, "done")

    def test_the_log_is_for_accountants_and_the_provider_key_is_out_of_reach(self):
        move = self.new_bill()
        self.read_bill(move)
        log = self.env["account.bill.capture.log"].with_user(self.accountant).search([("move_id", "=", move.id)])
        self.assertTrue(log)
        with self.assertRaises(AccessError):
            log.write({"state": "ok"})
        with self.assertRaises(AccessError):
            self.env["account.bill.capture.log"].with_user(self.plain_user).search([])
        with self.assertRaises(AccessError):
            self.provider.with_user(self.accountant).api_key
