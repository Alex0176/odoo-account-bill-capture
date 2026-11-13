# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo.tests.common import tagged

from .common import BillCase, ScriptedBillProvider, answer


class PriceCase(BillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.account = cls.env["account.account"].create({
            "name": "Test expense prices", "code": "999444", "account_type": "expense",
            "company_ids": [(6, 0, cls.company.ids)]})
        cls.other = cls.partner_ctx.create({"name": "Beispiel Gas AG", "is_company": True, "vat": "ATU00000002"})
        cls.tax = cls.env["account.tax"].search([("company_id", "=", cls.company.id), ("type_tax_use", "=", "purchase"),
                                                 ("amount", "=", 20.0)], limit=1)

    def setUp(self):
        super().setUp()
        self.units = self.env.ref("uom.product_uom_unit")
        self.dozen = self.env.ref("uom.product_uom_dozen")
        self.product = self.env["product.product"].create({
            "name": "Testartikel Prices", "type": "consu", "purchase_ok": True, "sale_ok": True,
            "list_price": 20.0, "standard_price": 5.0, "company_id": self.company.id})
        self.seller = self.env["product.supplierinfo"].create({
            "partner_id": self.supplier.id, "product_tmpl_id": self.product.product_tmpl_id.id,
            "price": 8.0, "min_qty": 1.0, "currency_id": self.company.currency_id.id, "company_id": self.company.id})
        self.Log = self.env["account.bill.capture.log"]

    def bill(self, price, qty=1.0, partner=None, discount=0.0, uom=None, move_type="in_invoice", product=None,
             post=True, currency=None, date="2026-03-01"):
        move = self.Move.create({
            "move_type": move_type, "journal_id": self.journal.id, "company_id": self.company.id,
            "partner_id": (partner or self.supplier).id, "invoice_date": date, "ref": "P-%s-%s" % (price, qty),
            "currency_id": (currency or self.company.currency_id).id,
            "invoice_line_ids": [(0, 0, {
                "name": "line", "product_id": (product or self.product).id, "quantity": qty, "price_unit": price,
                "discount": discount, "product_uom_id": (uom or self.units).id, "account_id": self.account.id,
                "tax_ids": [(6, 0, self.tax.ids)]})]})
        if post:
            move.action_post()
        return move

    def notes(self):
        return " ".join(str(b) for b in self.product.product_tmpl_id.message_ids.mapped("body"))

    def price_logs(self, move=None, state="applied"):
        domain = [("kind", "=", "price"), ("state", "=", state)]
        if move:
            domain.append(("move_id", "=", move.id))
        return self.Log.search(domain)


@tagged("post_install", "-at_install")
class TestVendorPrices(PriceCase):
    # ------------------------------------------------------------ the basic rule
    def test_same_price_changes_nothing(self):
        move = self.bill(8.0)
        self.assertEqual(self.seller.price, 8.0)
        self.assertFalse(self.price_logs(move))
        self.assertEqual(self.seller.bill_last_move_id, move)            # the last bill is still remembered

    def test_a_different_price_is_corrected_and_logged_and_noted(self):
        move = self.bill(9.0)
        self.assertEqual(self.seller.price, 9.0)
        log = self.price_logs(move)
        self.assertEqual((log.old_price, log.new_price, log.partner_id, log.created_seller), (8.0, 9.0, self.supplier, False))
        self.assertEqual(str(self.seller.bill_last_date), "2026-03-01")
        note = self.notes()
        for fragment in ("8.0", "9.0", "Muster-Strom GmbH", "data-oe-model=\"account.move\"", self.env.user.name):
            self.assertIn(fragment, note)

    def test_the_selling_price_and_the_cost_are_never_touched_by_default(self):
        self.bill(9.0)
        self.assertEqual(self.product.list_price, 20.0)
        self.assertEqual(self.product.standard_price, 5.0)

    def test_a_new_supplier_gets_an_additional_entry_and_the_old_one_stays(self):
        move = self.bill(7.5, partner=self.other)
        sellers = self.product.product_tmpl_id.seller_ids
        self.assertEqual(len(sellers), 2)
        self.assertEqual(self.seller.price, 8.0)
        new = sellers - self.seller
        self.assertEqual((new.partner_id, new.price, new.min_qty), (self.other, 7.5, 1.0))
        self.assertGreater(new.sequence, self.seller.sequence)           # new ones go to the back
        self.assertEqual({s.partner_id for s in sellers}, {self.supplier, self.other})
        self.assertTrue(self.price_logs(move).created_seller)
        self.bill(7.5, partner=self.other)                               # the same price again: no third entry
        self.assertEqual(len(self.product.product_tmpl_id.seller_ids), 2)

    def test_two_suppliers_keep_their_own_prices(self):
        self.bill(7.5, partner=self.other)
        self.bill(9.0)
        prices = {s.partner_id: s.price for s in self.product.product_tmpl_id.seller_ids}
        self.assertEqual(prices, {self.supplier: 9.0, self.other: 7.5})

    def test_the_vendor_article_number_is_taken_for_a_new_entry(self):
        move = self.bill(7.5, partner=self.other, post=False)
        move.invoice_line_ids.bill_ai_item_number = "GAS-77"
        move.action_post()
        self.assertEqual((self.product.product_tmpl_id.seller_ids - self.seller).product_code, "GAS-77")

    def test_tiered_prices(self):
        tier10 = self.env["product.supplierinfo"].create({
            "partner_id": self.supplier.id, "product_tmpl_id": self.product.product_tmpl_id.id,
            "price": 7.0, "min_qty": 10.0, "currency_id": self.company.currency_id.id, "company_id": self.company.id})
        self.bill(7.5, qty=12.0)
        self.assertEqual((self.seller.price, tier10.price), (8.0, 7.5))
        self.bill(8.5, qty=3.0)
        self.assertEqual((self.seller.price, tier10.price), (8.5, 7.5))

    def test_a_quantity_below_every_tier_gets_its_own_entry(self):
        self.seller.min_qty = 10.0
        self.bill(9.0, qty=2.0)
        self.assertEqual(len(self.product.product_tmpl_id.seller_ids), 2)
        self.assertEqual(self.seller.price, 8.0)

    def test_unit_conversion(self):
        self.seller.product_uom_id = self.units
        self.bill(96.0, uom=self.dozen)                                  # 8 per piece: same
        self.assertEqual(self.seller.price, 8.0)
        self.bill(108.0, uom=self.dozen)                                 # 9 per piece
        self.assertEqual(self.seller.price, 9.0)
        self.seller.write({"product_uom_id": self.dozen, "price": 100.0})
        self.bill(10.0, qty=12.0, uom=self.units)                        # 120 per dozen
        self.assertAlmostEqual(self.seller.price, 120.0)

    def test_foreign_currency_is_converted_into_the_currency_of_the_entry(self):
        usd = self.env.ref("base.USD")
        usd.active = True
        self.env["res.currency.rate"].create({"currency_id": usd.id, "rate": 1.25, "name": "2026-01-01",
                                              "company_id": self.company.id})
        self.bill(10.0, currency=usd)                                    # 10 USD = 8 EUR: same
        self.assertEqual(self.seller.price, 8.0)
        self.bill(12.5, currency=usd)                                    # = 10 EUR
        self.assertAlmostEqual(self.seller.price, 10.0)
        self.assertEqual(self.seller.currency_id, self.company.currency_id)

    def test_discount_counts(self):
        self.bill(10.0, discount=20.0)                                   # 8 after discount: same
        self.assertEqual(self.seller.price, 8.0)
        self.bill(10.0, discount=10.0)                                   # 9
        self.assertEqual(self.seller.price, 9.0)

    def test_what_is_never_a_price(self):
        credit = self.bill(5.0, move_type="in_refund")
        self.assertEqual(self.seller.price, 8.0)
        zero = self.Move.create({
            "move_type": "in_invoice", "journal_id": self.journal.id, "partner_id": self.supplier.id,
            "invoice_date": "2026-03-01", "invoice_line_ids": [
                (0, 0, {"name": "free", "product_id": self.product.id, "quantity": 1, "price_unit": 0.0,
                        "account_id": self.account.id}),
                (0, 0, {"name": "none", "product_id": self.product.id, "quantity": 0, "price_unit": 5.0,
                        "account_id": self.account.id}),
                (0, 0, {"name": "no product", "quantity": 1, "price_unit": 3.0, "account_id": self.account.id})]})
        zero.action_post()
        self.assertEqual(self.seller.price, 8.0)
        self.assertFalse(self.price_logs())
        self.assertFalse(credit.bill_ai_price_applied)

    # ------------------------------------------------------------ the limit
    def test_a_large_change_is_suggested_not_applied_and_a_click_confirms_it(self):
        move = self.bill(20.0)                                           # +150 %
        self.assertEqual(self.seller.price, 8.0)
        self.assertTrue(move.bill_ai_price_pending)
        pending = self.price_logs(move, "pending")
        self.assertEqual((pending.old_price, pending.new_price), (8.0, 20.0))
        self.assertTrue(any("confirm it on the bill" in (m.body or "") for m in self.product.product_tmpl_id.message_ids))
        move.with_user(self.accountant).action_bill_confirm_price_changes()
        self.assertEqual(self.seller.price, 20.0)
        self.assertFalse(move.bill_ai_price_pending)
        self.assertTrue(move.bill_ai_price_applied)
        self.assertEqual(self.price_logs(move).new_price, 20.0)

    def test_a_pending_change_can_be_dismissed(self):
        move = self.bill(20.0)
        move.action_bill_dismiss_price_changes()
        self.assertEqual(self.seller.price, 8.0)
        self.assertFalse(move.bill_ai_price_pending)

    def test_the_limit_is_configurable(self):
        self.company.bill_price_change_limit = 5.0
        move = self.bill(9.0)                                            # +12.5 %
        self.assertTrue(move.bill_ai_price_pending)
        self.company.bill_price_change_limit = 0.0
        move = self.bill(8.5)
        self.assertTrue(move.bill_ai_price_pending)

    # ------------------------------------------------------------ the setting
    def test_off_changes_nothing(self):
        self.company.bill_supplier_price_update = "off"
        move = self.bill(9.0)
        self.assertEqual(self.seller.price, 8.0)
        self.assertFalse(self.price_logs(move))

    def test_a_draft_does_not_change_prices_in_the_default_mode(self):
        move = self.bill(9.0, post=False)
        move._bill_update_supplier_prices  # exists
        self.assertEqual(self.seller.price, 8.0)
        move.action_post()
        self.assertEqual(self.seller.price, 9.0)

    def test_instant_changes_the_price_with_the_ai_proposal_and_not_again_at_posting(self):
        self.company.bill_supplier_price_update = "instant"
        self.post_history(self.supplier, "Testartikel Prices", product=self.product, account=self.account,
                          taxes=self.tax, price=8.0)
        move = self.new_bill()
        lines = [{"description": "Testartikel Prices", "quantity": 1, "unit_price_net": 9.0, "tax_rate": 20, "art": "ware"}]
        self.read_bill(move, answer(lines=lines, total_net=9.0, total_tax=1.8, total_gross=10.8))
        self.assertEqual(move.state, "draft")
        self.assertEqual(self.seller.price, 9.0)
        log = self.price_logs(move)
        self.assertEqual(log.origin, "instant")
        self.assertIn("AI capture", self.notes())
        move.invoice_date = "2026-03-01"
        move.action_post()
        self.assertEqual(len(self.price_logs(move)), 1)

    def test_a_read_in_the_default_mode_waits_for_the_posting(self):
        self.post_history(self.supplier, "Testartikel Prices", product=self.product, account=self.account,
                          taxes=self.tax, price=8.0)
        move = self.new_bill()
        lines = [{"description": "Testartikel Prices", "quantity": 1, "unit_price_net": 9.0, "tax_rate": 20, "art": "ware"}]
        self.read_bill(move, answer(lines=lines, total_net=9.0, total_tax=1.8, total_gross=10.8))
        self.assertEqual(self.seller.price, 8.0)

    # ------------------------------------------------------------ undo
    def test_undo_restores_a_changed_price(self):
        self.seller.write({"date_start": "2026-01-01"})
        move = self.bill(9.0)
        self.assertEqual(str(self.seller.date_start), "2026-03-01")
        move.with_user(self.accountant).action_bill_revert_price_changes()
        self.assertEqual(self.seller.price, 8.0)
        self.assertEqual(str(self.seller.date_start), "2026-01-01")
        self.assertEqual(self.price_logs(move, "reverted").old_price, 8.0)
        self.assertFalse(move.bill_ai_price_applied)
        self.assertIn("undone", self.notes())

    def test_undo_removes_an_entry_the_bill_created(self):
        move = self.bill(7.5, partner=self.other)
        move.action_bill_revert_price_changes()
        self.assertEqual(self.product.product_tmpl_id.seller_ids, self.seller)

    def test_undo_leaves_a_price_alone_that_was_changed_again(self):
        move = self.bill(9.0)
        self.seller.price = 9.5
        move.action_bill_revert_price_changes()
        self.assertEqual(self.seller.price, 9.5)

    # ------------------------------------------------------------ the cost of the product
    def test_the_cost_of_a_stocked_product_is_never_changed(self):
        self.company.bill_update_standard_price = True
        if "is_storable" not in self.product._fields:
            self.skipTest("stock is not installed")
        self.product.is_storable = True
        self.bill(9.0)
        self.assertEqual(self.product.standard_price, 5.0)
        self.assertEqual(self.seller.price, 9.0)

    def test_the_cost_of_a_product_without_stock_follows_when_the_setting_is_on_and_is_restored(self):
        if "is_storable" in self.product._fields:
            self.product.is_storable = False
        self.bill(9.0)
        self.assertEqual(self.product.standard_price, 5.0)               # setting is off by default
        self.company.bill_update_standard_price = True
        move = self.bill(9.5)
        self.assertAlmostEqual(self.product.with_company(self.company).standard_price, 9.5)
        move.action_bill_revert_price_changes()
        self.assertAlmostEqual(self.product.with_company(self.company).standard_price, 5.0)


@tagged("post_install", "-at_install")
class TestMargin(PriceCase):
    """The same fixtures; the margin warning."""

    def hint(self, move):
        return move.invoice_line_ids.bill_ai_margin_hint

    def test_a_loss_is_warned_about_with_the_figures(self):
        self.product.list_price = 8.0
        move = self.bill(9.0)
        self.assertTrue(move.bill_ai_low_margin)
        self.assertTrue(move.invoice_line_ids.bill_ai_margin_warning)
        hint = self.hint(move)
        self.assertIn("below the purchase price", hint)
        self.assertIn("8.00", hint)
        self.assertIn("9.00", hint)
        self.assertIn("-12", hint)
        self.assertIn("below the purchase price", self.notes())
        self.assertEqual(self.product.list_price, 8.0)                   # never changed by itself

    def test_a_small_margin_is_warned_about(self):
        self.product.list_price = 10.0
        move = self.bill(9.5)
        self.assertIn("below the minimum of 10 %", self.hint(move))
        self.assertTrue(move.bill_ai_low_margin)

    def test_a_sufficient_margin_is_not(self):
        move = self.bill(9.0)                                            # 20 selling, margin 55 %
        self.assertFalse(self.hint(move))
        self.assertFalse(move.bill_ai_low_margin)

    def test_threshold_zero_warns_about_a_loss_only(self):
        self.company.bill_min_margin_percent = 0.0
        self.product.list_price = 10.0
        self.assertFalse(self.hint(self.bill(9.5)))
        self.product.list_price = 9.0
        self.assertTrue(self.hint(self.bill(9.5)))

    def test_the_switch_turns_it_off(self):
        self.company.bill_margin_check = False
        self.product.list_price = 5.0
        move = self.bill(9.0)
        self.assertFalse(self.hint(move))
        self.assertFalse(move.bill_ai_low_margin)

    def test_the_unit_of_the_line_is_converted_to_the_selling_unit(self):
        self.product.list_price = 11.0
        move = self.bill(120.0, uom=self.dozen)                          # 10 per piece: margin 9 %
        self.assertIn("below the minimum", self.hint(move))
        self.product.list_price = 20.0
        self.assertFalse(self.hint(self.bill(120.0, uom=self.dozen)))

    def test_a_product_that_is_not_for_sale_gets_no_warning(self):
        self.product.write({"sale_ok": False, "list_price": 1.0})
        move = self.bill(9.0)
        self.assertFalse(self.hint(move))
        self.assertFalse(move.bill_ai_low_margin)

    def test_a_product_without_a_selling_price_gets_a_short_hint_only(self):
        self.product.list_price = 0.0
        move = self.bill(9.0)
        self.assertEqual(self.hint(move), "No selling price set")
        self.assertFalse(move.bill_ai_low_margin)

    def test_the_warning_is_also_given_with_the_ai_proposal(self):
        self.product.list_price = 8.0
        self.post_history(self.supplier, "Testartikel Prices", product=self.product, account=self.account,
                          taxes=self.tax, price=8.0)
        move = self.new_bill()
        lines = [{"description": "Testartikel Prices", "quantity": 1, "unit_price_net": 9.0, "tax_rate": 20, "art": "ware"}]
        self.read_bill(move, answer(lines=lines, total_net=9.0, total_tax=1.8, total_gross=10.8))
        self.assertTrue(move.bill_ai_low_margin)
        self.assertEqual(self.product.list_price, 8.0)

    def test_the_selling_price_wizard_changes_the_price_only_after_the_click(self):
        self.product.list_price = 8.0
        move = self.bill(9.0)
        line = move.invoice_line_ids
        action = line.with_user(self.accountant).action_bill_adjust_sale_price()
        wizard = self.env["account.bill.sale.price.wizard"].browse(action["res_id"])
        self.assertEqual(self.product.list_price, 8.0)
        self.assertAlmostEqual(wizard.cost, 9.0)
        self.assertGreater(wizard.new_price, 9.0)                         # a proposal from the markup
        wizard.markup_percent = 50.0
        wizard._onchange_markup_percent()
        self.assertAlmostEqual(wizard.new_price, 13.5)
        wizard.with_user(self.accountant).action_confirm()
        self.assertEqual(self.product.list_price, 13.5)
        self.assertFalse(line.bill_ai_margin_warning)

    def test_an_activity_for_the_responsible_user_sends_no_mail(self):
        self.company.bill_margin_activity_user_id = self.accountant
        self.product.list_price = 8.0
        mails = self.env["mail.mail"].search_count([])
        self.bill(9.0)
        activities = self.product.product_tmpl_id.activity_ids
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities.user_id, self.accountant)
        self.assertEqual(self.env["mail.mail"].search_count([]), mails)
        self.bill(9.5)
        self.assertEqual(len(self.product.product_tmpl_id.activity_ids), 1)    # not twice
