# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Shared test setup. Everything is invented: companies, suppliers, products, numbers."""
import base64
import copy
import json

from odoo.addons.ai_translation_consensus import providers
from odoo.addons.ai_translation_consensus.providers.base import ChatProvider
from odoo.addons.ai_translation_consensus.providers.http import ProviderError, ProviderUnavailable
from odoo.tests.common import TransactionCase

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 40
HEIC = b"\x00\x00\x00\x18ftypheic" + b"0" * 40

BASE_ANSWER = {
    "supplier": {"name": "Muster-Strom GmbH", "vat": None, "iban": None, "address": None},
    "invoice_number": "RE-2026-001",
    "invoice_date": None,
    "due_date": None,
    "currency": None,
    "lines": [{"description": "Elektrische Energie Oktober", "item_number": None, "quantity": 1,
               "unit": None, "unit_price_net": 100.0, "discount_percent": 0, "tax_rate": 20,
               "art": "laufende_kosten"}],
    "total_net": 100.0, "total_tax": 20.0, "total_gross": 120.0,
    "payment_reference": None,
    "confidence": {"supplier": 0.9, "invoice_number": 0.95, "totals": 0.99},
}


def answer(**changes):
    data = copy.deepcopy(BASE_ANSWER)
    for key, value in changes.items():
        if key == "supplier":
            data["supplier"].update(value)
        else:
            data[key] = value
    return data


class ScriptedBillProvider(ChatProvider):
    """Provider kind for the tests: returns ``ScriptedBillProvider.reply`` instead of calling a service."""

    kind = "scripted_bill"
    label = "Scripted bill reader (tests)"
    needs_key = False
    can_read_documents = True
    defaults = {}
    reply = None            # str or dict
    raises = None           # an exception instance
    calls = []

    def _complete_document(self, system, user, files):
        ScriptedBillProvider.calls.append({"name": self.name, "system": system, "user": user, "files": files})
        if ScriptedBillProvider.raises:
            raise ScriptedBillProvider.raises
        self._tokens(900, 300)
        reply = ScriptedBillProvider.reply
        return reply if isinstance(reply, str) else json.dumps(reply)


class BillCase(TransactionCase):
    """A company with an Austrian chart, a purchase journal, an accountant and a scripted provider."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        providers.REGISTRY["scripted_bill"] = ScriptedBillProvider
        cls.addClassCleanup(providers.REGISTRY.pop, "scripted_bill", None)
        austria = cls.env.ref("base.at")
        company_partner = cls.env["res.partner"].create({"name": "Muster Handels GmbH", "is_company": True,
                                                         "country_id": austria.id})
        cls.company = cls.env["res.company"].create({
            "name": "Muster Handels GmbH", "partner_id": company_partner.id, "country_id": austria.id,
            "currency_id": cls.env.ref("base.EUR").id})
        cls.env["account.chart.template"].try_loading("at", company=cls.company, install_demo=False)
        cls.env.user.company_ids |= cls.company
        cls.env = cls.env(context=dict(cls.env.context, allowed_company_ids=[cls.company.id]))
        cls.Move = cls.env["account.move"]
        cls.Provider = cls.env["ai.translation.provider"]
        cls.Provider.search([]).write({"active": False})
        purposes = cls.env["ai.translation.purpose"].search([("code", "in", ["invoice_manual", "invoice_auto"])])
        cls.provider = cls.Provider.create({
            "name": "Scripted reader A", "kind": "scripted_bill", "is_local": True, "sequence": 1,
            "purpose_ids": [(6, 0, purposes.ids)]})
        cls.journal = cls.env["account.journal"].search(
            [("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1)
        cls.partner_ctx = cls.env["res.partner"].with_context(no_vat_validation=True)
        cls.supplier = cls.partner_ctx.create({
            "name": "Muster-Strom GmbH", "is_company": True, "vat": "ATU00000001", "supplier_rank": 1,
            "company_id": False})
        cls.accountant = cls.env["res.users"].create({
            "name": "Test accountant", "login": "test_accountant_bill", "company_id": cls.company.id,
            "company_ids": [(6, 0, cls.company.ids)],
            "group_ids": [(6, 0, [cls.env.ref("account.group_account_invoice").id])]})
        cls.plain_user = cls.env["res.users"].create({
            "name": "Test employee", "login": "test_employee_bill", "company_id": cls.company.id,
            "company_ids": [(6, 0, cls.company.ids)],
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])]})

    def setUp(self):
        super().setUp()
        ScriptedBillProvider.calls = []
        ScriptedBillProvider.raises = None
        ScriptedBillProvider.reply = answer()
        self.company.write({"bill_capture_mode": "manual", "bill_unknown_product_mode": "suggest",
                            "bill_supplier_price_update": "post", "bill_margin_check": True,
                            "bill_min_margin_percent": 10.0, "bill_price_change_limit": 50.0})

    # -- helpers
    def new_bill(self, raw=PDF, name="bill.pdf", partner=None, **vals):
        move = self.Move.create(dict({
            "move_type": "in_invoice", "journal_id": self.journal.id, "company_id": self.company.id,
            "partner_id": partner.id if partner else False}, **vals))
        self.env["ir.attachment"].create({"name": name, "raw": raw, "res_model": "account.move", "res_id": move.id})
        return move

    def read_bill(self, move, reply=None, code="invoice_manual", origin="manual"):
        if reply is not None:
            ScriptedBillProvider.reply = reply
        return move._bill_ai_run(code, origin)

    def tax(self, rate, name=None, **vals):
        return self.env["account.tax"].create(dict({
            "name": name or "Test tax %s" % rate, "amount": rate, "amount_type": "percent",
            "type_tax_use": "purchase", "company_id": self.company.id}, **vals))

    def expense_account(self, code="999001"):
        return self.env["account.account"].search([("code", "=", code), ("company_ids", "in", self.company.id)], limit=1) \
            or self.env["account.account"].create({"name": "Test expense %s" % code, "code": code,
                                                   "account_type": "expense", "company_ids": [(6, 0, self.company.ids)]})

    def post_history(self, partner, description, product=None, account=None, taxes=None, price=10.0, qty=1.0, ref="H-1"):
        """A posted vendor bill of the supplier: the history the proposal learns from."""
        account = account or self.expense_account()
        move = self.Move.create({
            "move_type": "in_invoice", "journal_id": self.journal.id, "company_id": self.company.id,
            "partner_id": partner.id, "invoice_date": "2026-01-15", "ref": ref,
            "invoice_line_ids": [(0, 0, {
                "name": description, "product_id": product.id if product else False, "quantity": qty,
                "price_unit": price, "account_id": account.id,
                "tax_ids": [(6, 0, (taxes or self.env["account.tax"]).ids)]})]})
        move.action_post()
        return move
