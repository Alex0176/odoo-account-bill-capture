# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo.tests.common import tagged

from .common import BillCase, ScriptedBillProvider, answer

LINES = [
    {"description": "Schraube M6 Edelstahl", "item_number": "SCR-M6", "quantity": 100, "unit": None,
     "unit_price_net": 0.25, "discount_percent": 0, "tax_rate": 20, "art": "ware"},
    {"description": "Montage vor Ort", "item_number": None, "quantity": 2, "unit": None,
     "unit_price_net": 60.0, "discount_percent": 0, "tax_rate": 20, "art": "dienstleistung"},
    {"description": "Strom Oktober Arbeitspreis", "item_number": None, "quantity": 1, "unit": None,
     "unit_price_net": 80.0, "discount_percent": 0, "tax_rate": 20, "art": "laufende_kosten"},
]
TOTALS = dict(total_net=225.0, total_tax=45.0, total_gross=270.0)


@tagged("post_install", "-at_install")
class TestUnknownProducts(BillCase):
    def setUp(self):
        super().setUp()
        self.account = self.expense_account("999333")
        self.post_history(self.supplier, "Something old", account=self.account)
        self.Product = self.env["product.product"].with_context(active_test=False)

    def read(self, lines=LINES, **kw):
        move = self.new_bill()
        self.read_bill(move, answer(lines=lines, **TOTALS), **kw)
        return move

    def products_named(self, name):
        return self.Product.search([("name", "=", name)])

    def check_safe(self, product):
        """Never for sale, never published, never stocked, no variants."""
        self.assertFalse(product.sale_ok)
        self.assertTrue(product.purchase_ok)
        if "is_storable" in product._fields:
            self.assertFalse(product.is_storable)
        if "is_published" in product._fields:
            self.assertFalse(product.is_published)
        self.assertEqual(product.product_tmpl_id.product_variant_count, 1)

    # ------------------------------------------------------------ option 1: only suggest
    def test_suggest_leaves_the_product_out_and_offers_a_button(self):
        before = self.Product.search_count([])
        move = self.read()
        self.assertEqual(self.Product.search_count([]), before)
        lines = move.invoice_line_ids.sorted("id")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].bill_ai_product_missing and lines[1].bill_ai_product_missing)
        self.assertFalse(lines[2].bill_ai_product_missing)               # running costs: a general line
        self.assertEqual(lines[0].account_id, self.account)
        self.assertIn("Product missing", lines[0].bill_ai_hint)
        self.assertTrue(move.bill_ai_product_missing)
        # the button creates exactly this line's product
        lines[0].with_user(self.accountant).action_bill_create_product()
        product = lines[0].product_id
        self.assertEqual(product.name, "Schraube M6 Edelstahl")
        self.check_safe(product)
        self.assertTrue(product.bill_ai_created)
        self.assertFalse(lines[0].bill_ai_product_missing)
        self.assertTrue(move.bill_ai_product_missing)                    # the service line still misses one
        self.assertEqual(self.Product.search_count([]), before + 1)

    # ------------------------------------------------------------ option 2: create
    def test_create_makes_goods_and_services_but_not_running_costs(self):
        self.company.bill_unknown_product_mode = "create"
        move = self.read()
        lines = move.invoice_line_ids.sorted("id")
        goods, service, power = lines[0].product_id, lines[1].product_id, lines[2].product_id
        self.assertTrue(goods and service)
        self.assertFalse(power)                                          # electricity: a general line
        self.assertEqual((goods.type, service.type), ("consu", "service"))
        for product in (goods, service):
            self.check_safe(product)
            self.assertTrue(product.bill_ai_created)
            self.assertEqual(product.bill_ai_move_id, move)
        self.assertEqual(goods.property_account_expense_id, self.account)
        self.assertTrue(move.bill_ai_new_product)
        self.assertIn("Product created by AI", lines[0].bill_ai_hint)

    def test_create_adds_the_vendor_entry_with_article_number_and_net_price(self):
        self.company.bill_unknown_product_mode = "create"
        move = self.read()
        goods = move.invoice_line_ids.sorted("id")[0].product_id
        seller = goods.seller_ids
        self.assertEqual((seller.partner_id, seller.price, seller.product_code, seller.min_qty),
                         (self.supplier, 0.25, "SCR-M6", 1.0))
        self.assertEqual(seller.currency_id, move.currency_id)

    def test_create_never_duplicates(self):
        self.company.bill_unknown_product_mode = "create"
        self.read()
        self.read()                                                      # the same bill again, another draft
        self.assertEqual(len(self.products_named("Schraube M6 Edelstahl")), 1)
        self.assertEqual(len(self.products_named("Montage vor Ort")), 1)

    def test_an_existing_product_is_found_by_number_reference_or_name_before_anything_is_created(self):
        self.company.bill_unknown_product_mode = "create"
        by_ref = self.Product.create({"name": "Screws stainless", "default_code": "SCR-M6", "purchase_ok": True})
        by_name = self.Product.create({"name": "Montage vor Ort", "purchase_ok": True, "type": "service"})
        before = self.Product.search_count([])
        move = self.read()
        lines = move.invoice_line_ids.sorted("id")
        self.assertEqual(lines[0].product_id, by_ref)
        self.assertEqual(lines[1].product_id, by_name)
        self.assertEqual(self.Product.search_count([]), before)
        self.assertFalse(move.bill_ai_new_product)

    def test_the_vendor_article_number_finds_the_product(self):
        self.company.bill_unknown_product_mode = "create"
        product = self.Product.create({"name": "Totally different name", "purchase_ok": True})
        self.env["product.supplierinfo"].create({
            "partner_id": self.supplier.id, "product_tmpl_id": product.product_tmpl_id.id, "price": 1.0,
            "product_code": "scr-m6"})
        move = self.read()
        self.assertEqual(move.invoice_line_ids.sorted("id")[0].product_id, product)

    def test_a_product_that_cannot_be_purchased_is_not_picked(self):
        self.Product.create({"name": "Montage vor Ort", "purchase_ok": False, "type": "service"})
        move = self.read()
        self.assertFalse(move.invoice_line_ids.sorted("id")[1].product_id)

    def test_automatic_mode_with_create_makes_products_but_never_posts(self):
        self.company.write({"bill_capture_mode": "auto", "bill_unknown_product_mode": "create"})
        self.provider.purpose_ids = self.env.ref("ai_translation_consensus.purpose_invoice_auto")
        ScriptedBillProvider.reply = answer(lines=LINES, **TOTALS)
        move = self.new_bill()
        self.env["account.move"]._cron_bill_capture()
        self.assertEqual(move.bill_ai_state, "done")
        self.assertEqual(move.state, "draft")
        self.assertTrue(self.products_named("Schraube M6 Edelstahl"))
        self.assertEqual(self.Move.search_count([("id", "=", move.id), ("state", "=", "posted")]), 0)
        self.provider.purpose_ids |= self.env.ref("ai_translation_consensus.purpose_invoice_manual")

    # ------------------------------------------------------------ option 3: general line
    def test_general_line_has_text_and_account_but_no_product(self):
        self.company.bill_unknown_product_mode = "general"
        before = self.Product.search_count([])
        move = self.read()
        self.assertEqual(self.Product.search_count([]), before)
        for line in move.invoice_line_ids:
            self.assertFalse(line.product_id)
            self.assertEqual(line.account_id, self.account)
            self.assertFalse(line.bill_ai_product_missing)
        self.assertFalse(move.bill_ai_product_missing)
        self.assertEqual(move.invoice_line_ids.sorted("id")[2].name, "Strom Oktober Arbeitspreis")

    def test_general_line_with_a_collective_product_per_expense_account(self):
        self.company.write({"bill_unknown_product_mode": "general", "bill_general_product": True})
        move = self.read()
        products = move.invoice_line_ids.product_id
        self.assertEqual(len(products), 1)                               # one account, one collective product
        self.assertEqual(products.type, "service")
        self.check_safe(products)
        self.assertFalse(products.bill_ai_created)
        self.assertEqual(products.property_account_expense_id, self.account)
        move2 = self.read()
        self.assertEqual(move2.invoice_line_ids.product_id, products)    # reused, not duplicated

    def test_running_costs_are_general_lines_in_the_create_mode_too_and_a_service_is_never_stocked(self):
        self.company.write({"bill_unknown_product_mode": "create"})
        lines = [LINES[2], dict(LINES[2], description="Miete Lager November", unit_price_net=500.0)]
        move = self.new_bill()
        self.read_bill(move, answer(lines=lines, total_net=580.0, total_tax=116.0, total_gross=696.0))
        self.assertFalse(move.invoice_line_ids.product_id)
        self.assertFalse(move.bill_ai_new_product)

    def test_mark_a_created_product_as_reviewed(self):
        self.company.bill_unknown_product_mode = "create"
        move = self.read()
        product = move.invoice_line_ids.sorted("id")[0].product_id
        product.product_tmpl_id.action_bill_ai_reviewed()
        self.assertFalse(product.bill_ai_created)
        move._bill_ai_refresh_flags()
        self.assertTrue(move.bill_ai_new_product)                        # the service product is still open
