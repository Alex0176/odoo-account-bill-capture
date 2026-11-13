# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    bill_ai_created = fields.Boolean(
        string="Created by AI, to review", copy=False, index=True,
        help="Created from a vendor bill by the AI capture. Check name, category and account, "
             "then mark it as reviewed.")
    bill_ai_move_id = fields.Many2one("account.move", string="Created from bill", copy=False,
                                      ondelete="set null")

    def action_bill_ai_reviewed(self):
        self.write({"bill_ai_created": False})


class ProductSupplierinfo(models.Model):
    _inherit = "product.supplierinfo"

    bill_last_move_id = fields.Many2one("account.move", string="Last bill", copy=False, ondelete="set null")
    bill_last_date = fields.Date(string="Last bill date", copy=False)
    bill_last_price = fields.Float(string="Last billed price", digits="Product Price", copy=False)
