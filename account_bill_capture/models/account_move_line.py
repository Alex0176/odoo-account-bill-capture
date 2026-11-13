# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    bill_ai_generated = fields.Boolean(copy=False,
                                       help="Created by the AI capture; a new reading replaces these lines only.")
    bill_ai_art = fields.Selection([("ware", "Goods"), ("dienstleistung", "Service"),
                                    ("laufende_kosten", "Running costs")], string="Kind (AI)", copy=False)
    bill_ai_item_number = fields.Char(string="Supplier article number", copy=False)
    bill_ai_product_missing = fields.Boolean(string="Product missing", copy=False)
    bill_ai_margin_warning = fields.Boolean(copy=False)
    bill_ai_hint = fields.Char(string="Hint (AI)", copy=False)
    bill_ai_margin_hint = fields.Char(string="Selling price hint", copy=False)

    def action_bill_create_product(self):
        """Create the missing product of this line (only after a click: a person decides)."""
        self.ensure_one()
        move = self.move_id
        if move.state != "draft" or move.move_type != "in_invoice":
            raise UserError(_("Only the lines of a draft vendor bill can get a product."))
        if self.product_id:
            raise UserError(_("The line already has a product."))
        move._bill_ai_check_rights()
        product = move._bill_ai_find_or_create_product(
            self, create=True, partner=move.partner_id, art=self.bill_ai_art or "dienstleistung")
        if product:
            self.write({"product_id": product.id, "bill_ai_product_missing": False,
                        "bill_ai_hint": False})
        return True

    def action_bill_adjust_sale_price(self):
        self.ensure_one()
        return self.env["account.bill.sale.price.wizard"]._open_for(self)
